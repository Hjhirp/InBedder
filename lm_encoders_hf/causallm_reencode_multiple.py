"""
re-encode multiple times
"""
import os, logging, json
from tqdm import trange
from typing import List, Union, Dict
from collections import defaultdict

import torch
import numpy as np
from torch import Tensor, device
from numpy import ndarray
from safetensors import safe_open
from safetensors.torch import save_file
from transformers import AutoTokenizer, AutoModelForCausalLM

logger = logging.getLogger(__name__)

CAUSAL_OUTPUTVALUES = ["re_encode"]

def verify_output_value(output_value: str):
    for ov in CAUSAL_OUTPUTVALUES:
        if output_value.startswith(ov):
            return True
    return False

class CausalLMEncoderMultiple:
    """implement convenient encode function that has similar appearance as sentence-transformers"""
    def __init__(self, model_name_or_path: str,
                tokenizer_path: str = None,
                output_value: str = "re_encode",
                max_input_length: int = 512,
                dtype: str = "float32",
                use_flash_attention_2: bool = False,
                cache_embeddings: bool = True,
                last_layer_only: bool = False,
                reencoder=None,
                reencode_times=10,
                **generation_configs):
        """
        :param model_name_or_path: dir to converted hf weights
        :param tokenizer_path: path to tokenizer, if not specified use model_name_or_path
        :param output_value: the hidden states you want to output from LLM (choose from existing options)
        :param dtype: specify dtype of model parameters
        :param use_flash_attention_2: only certain architectures can use flash attention 2, see all supported ones in https://github.com/huggingface/transformers/issues/26350
        :param reencoder: encoder that re-encodes the generated texts
        :param generation_configs: configs for generation
        """
        if tokenizer_path is None:
            tokenizer_path = model_name_or_path
        
        # all causal lms have left padding during inference
        # during training it can use either left or right padding
        # truncation must be done on the left, because the pattern is input+instruction+response
        # it will only truncate input in this case
        self.tokenizer = AutoTokenizer.from_pretrained(tokenizer_path, padding_side='left', truncation_side="left")
        
        logger.info(f"Use dtype {dtype}")
        logger.info(f"Loading model from {model_name_or_path} ...")
        self.model = AutoModelForCausalLM.from_pretrained(model_name_or_path,
                                                          torch_dtype=torch.bfloat16 if dtype == "bfloat16" else torch.float32,
                                                          low_cpu_mem_usage=True,
                                                          trust_remote_code=True,
                                                          use_flash_attention_2=use_flash_attention_2,
                                                          device_map="auto") # flash attention 2 is a speed-up implementation of attention module
        
        assert verify_output_value(output_value), f"{output_value} is not defined, please choose from {CAUSAL_OUTPUTVALUES}"

        self.output_value = output_value
        logger.info(f"Using aggregation method {self.output_value}")
        self.max_input_length = max_input_length
        self.cache_embeddings = cache_embeddings
        if self.cache_embeddings:
            logger.warning("Embeddings will take up too much memory!")
        self.reencoder = reencoder
        self.generation_configs = generation_configs
        self.last_layer_only = last_layer_only
        if self.last_layer_only:
            logger.info("Will only use last layer hidden states.")
        self.reencode_times = reencode_times

        if self.tokenizer.pad_token_id == None:
            self.tokenizer.pad_token_id = self.tokenizer.eos_token_id
            self.generation_configs['pad_token_id'] = self.tokenizer.eos_token_id
            
    def set_output_value(self, output_value: str = None) -> None:
        if self.output_value is not None:
            self.output_value = output_value
            logger.info(f"Using aggregation method {self.output_value}")
    
    def get_all_output_values(self) -> List[str]:
        # +1 token embedding
        num_layers = self.model.config.num_hidden_layers + 1
        all_output_values = []
        for ov in CAUSAL_OUTPUTVALUES:
            if ov.endswith("_"):
                all_output_values.extend([ov+str(l) for l in range(num_layers)])
            else:
                all_output_values.append(ov)
        return all_output_values
    
    def get_last_output_values(self) -> List[str]:
        return [ov + str(self.model.config.num_hidden_layers) if ov.endswith("_") else ov for ov in CAUSAL_OUTPUTVALUES]
    
    @torch.inference_mode()
    def encode(self, sentences: Union[str, List[str]],
               batch_size: int = 4,
               show_progress_bar: bool = None,
               convert_to_numpy: bool = True,
               convert_to_tensor: bool = False,
               normalize_embeddings: bool = False,
               return_generations: bool = False,
               cache_dir: str = None,
               filter_words: List[str] = None) -> Union[List[Tensor], ndarray, Tensor]:
        """
        Computes sentence embeddings

        :param sentences: The sentences to embed
        :param batch_size: The batch size used for the computation
        :param show_progress_bar: Output a progress bar when encode sentences
        :param convert_to_numpy: If true, the output is a list of numpy vectors. Else, it is a list of pytorch tensors.
        :param convert_to_tensor: If true, you get one large tensor as return. Overwrites any setting from convert_to_numpy
        :param normalize_embeddings: If set to true, returned vectors will have length 1. In that case, the faster dot-product (util.dot_score) instead of cosine similarity can be used.
        :param return_generations: If set to true, returned generation results.
        :param cache_dir: Default to None. If not None, then embeddings and generations will be saved the cache_dir.

        :return:
           By default, a list of tensors is returned. If convert_to_tensor, a stacked tensor is returned. If convert_to_numpy, a numpy matrix is returned.
        """
        self.model.eval()

        if show_progress_bar is None:
            show_progress_bar = (logger.getEffectiveLevel()==logging.INFO or logger.getEffectiveLevel()==logging.DEBUG)
        
        if convert_to_tensor:
            convert_to_numpy = False
        
        input_was_string = False
        if isinstance(sentences, str) or not hasattr(sentences, '__len__'): #Cast an individual sentence to a list with length 1
            sentences = [sentences]
            input_was_string = True
        
        device = self.model.device

        # process filter words
        if filter_words is not None:
            filter_ids = set()
            for filter_word in filter_words:
                filter_ids |= set(self.tokenizer.encode(filter_word))
            filter_ids = list(filter_ids)
        else:
            filter_ids = None

        all_embeddings, all_generations = None, None
        if cache_dir is not None and os.path.exists(cache_dir):
            # retrieve results
            try:
                all_embeddings = {}
                with safe_open(os.path.join(cache_dir, "embeds.safetensors"), framework="pt", device="cpu") as f:
                    for k in f.keys():
                        all_embeddings[k] = f.get_tensor(k)
                        assert len(all_embeddings[k]) == len(sentences)

                with open(os.path.join(cache_dir, 'generations.json'), 'r') as f:
                    all_generations = json.load(f)
                    assert len(all_generations) == len(sentences)
            except Exception as e:
                print(e)
        
        if all_embeddings is None or all_generations is None:
            # encode
            # sort according to length so that similar length are together
            all_embeddings = defaultdict(list)
            all_generations = []

            length_sorted_idx = np.argsort([-self._text_length(sen) for sen in sentences])
            sentences_sorted = [sentences[idx] for idx in length_sorted_idx]

            for start_index in trange(0, len(sentences), batch_size, desc="Batches", disable=not show_progress_bar):
                sentences_batch = sentences_sorted[start_index:start_index+batch_size]
                features = self.tokenizer(sentences_batch, padding=True, truncation=True, max_length=self.max_input_length, return_tensors="pt")
                features = batch_to_device(features, device)
                
                reencode_embeddings_multiple = []
                sub_generations = [[] for _ in range(len(sentences_batch))]
                for _ in range(self.reencode_times):
                    outputs = self.model.generate(**features, **self.generation_configs,
                                                    return_dict_in_generate=True, output_hidden_states=True, output_scores=True)

                    generations = self.tokenizer.batch_decode(outputs['sequences'], skip_special_tokens=True)
                    decoded_ids = outputs['sequences'][:, features['input_ids'].shape[1]:]

                    generations_remove_prompt = self.tokenizer.batch_decode(decoded_ids, skip_special_tokens=True)
                    # all_generations += generations
                    for bi, g in enumerate(generations):
                        sub_generations[bi].append(g)
                
                    aggregated = self._reencode_embeddings(generations_remove_prompt)
                    reencode_embeddings_multiple.append(aggregated)
                
                all_embeddings["re_encode"].append(torch.mean(torch.stack([e.unsqueeze(0) for e in reencode_embeddings_multiple], dim=0), dim=0).squeeze())
                all_generations += sub_generations
            
            # sort the list back to the original order
            for k in all_embeddings:
                all_embeddings[k] = torch.cat(all_embeddings[k], dim=0)
                all_embeddings[k] = torch.stack([all_embeddings[k][idx] for idx in np.argsort(length_sorted_idx)])
            all_generations = [all_generations[idx] for idx in np.argsort(length_sorted_idx)]

            # cache results
            if cache_dir is not None:
                os.makedirs(cache_dir, exist_ok=True)
                if self.cache_embeddings:
                    save_file(all_embeddings, os.path.join(cache_dir, "embeds.safetensors"))
                with open(os.path.join(cache_dir, 'generations.json'), 'w') as f:
                    json.dump(all_generations, f, indent=4)
            
        output_embeddings = all_embeddings[self.output_value]
        
        if normalize_embeddings:
            output_embeddings = torch.nn.functional.normalize(output_embeddings, p=2, dim=1)
        
        if convert_to_numpy:
            output_embeddings = output_embeddings.to(dtype=torch.float32).numpy()

        if input_was_string:
            output_embeddings = output_embeddings[0]
                
        if return_generations:
            return output_embeddings, all_generations
        else:
            return output_embeddings
    
    def _reencode_embeddings(self, generations):
        return torch.tensor(self.reencoder.encode(generations, show_progress_bar=False))
    
    def _text_length(self, text: Union[List[int], List[List[int]]]):
        """
        Help function to get the length for the input text. Text can be either
        a list of ints (which means a single text as input), or a tuple of list of ints
        (representing several text inputs to the model).
        """

        if isinstance(text, dict):              #{key: value} case
            return len(next(iter(text.values())))
        elif not hasattr(text, '__len__'):      #Object has no len() method
            return 1
        elif len(text) == 0 or isinstance(text[0], int):    #Empty string or list of ints
            return len(text)
        else:
            return sum([len(t) for t in text])      #Sum of length of individual strings
    
    # in order to utilize dense retrieval
    def encode_queries(self, queries: List[str], batch_size: int, **kwargs):
        return self.encode(queries, batch_size=batch_size, **kwargs)

    def encode_corpus(self, corpus: List[Dict[str, str]], batch_size: int, **kwargs):
        if type(corpus) is dict:
            sentences = [
                (corpus["title"][i] + " " + corpus["text"][i]).strip()
                if "title" in corpus
                else corpus["text"][i].strip()
                for i in range(len(corpus["text"]))
            ]
        elif type(corpus) is list and type(corpus[0]) is str:
            sentences = corpus
        else:
            sentences = [
                (doc["title"] + " " + doc["text"]).strip() if "title" in doc else doc["text"].strip()
                for doc in corpus
            ]
        return self.encode(sentences, batch_size=batch_size, **kwargs)


def batch_to_device(batch, target_device: device):
    """
    send a pytorch batch to a device (CPU/GPU)
    """
    for key in batch:
        if isinstance(batch[key], Tensor):
            batch[key] = batch[key].to(target_device)
    return batch
