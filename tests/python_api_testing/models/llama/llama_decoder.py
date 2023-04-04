import math
from pathlib import Path
import sys
f = f"{Path(__file__).parent}"
sys.path.append(f"{f}/..")
sys.path.append(f"{f}/../..")
sys.path.append(f"{f}/../../..")
sys.path.append(f"{f}/../../../..")

from loguru import logger
import torch
import numpy as np
from torch import nn, Tensor
from torch.nn import CrossEntropyLoss
from torch.utils.checkpoint import checkpoint
from libs import tt_lib as ttl
from typing import List, Optional, Tuple, Union

from transformers import T5Tokenizer, T5Model, AutoTokenizer, AutoModelForCausalLM
from collections import OrderedDict

from utility_functions import pad_activation, pad_weight, tilize_to_list, untilize, nearest_32, print_diff_argmax, tt2torch, tt2torch_rm
from fused_ops.linear import Linear as TtLinear
from fused_ops.softmax import softmax as TTsoftmax
from python_api_testing.models.llama.llama_utils import *
from python_api_testing.models.llama.llama_mlp import TtLlamaMLP
from python_api_testing.models.llama.llama_attention import TtLlamaAttention
from python_api_testing.models.llama.llama_layer_norm import TtLlamaRMSNorm


class TtLlamaDecoderLayer(nn.Module):
    def __init__(self, device, state_dict, decoder_idx, config):
        super().__init__()
        self.hidden_size = config.hidden_size
        self.state_dict = state_dict
        self.device = device
        self.decoder_idx = decoder_idx

        # PyTorch: self.self_attn = LlamaAttention(
        #     hidden_size=self.hidden_size,
        #     num_heads=config.num_attention_heads,
        # )
        self.self_attn = TtLlamaAttention(
            self.device,
            state_dict=self.state_dict,
            layer_num=decoder_idx,
            hidden_size=config.hidden_size,
            num_heads=config.num_attention_heads
        )

        # PyTorch: self.mlp = LlamaMLP(
        #     hidden_size=self.hidden_size,
        #     intermediate_size=config.intermediate_size,
        #     hidden_act=config.hidden_act,
        # )
        self.mlp = TtLlamaMLP(
            self.device,
            state_dict=self.state_dict,
            layer_num=decoder_idx,
            hidden_size=self.hidden_size,
            intermediate_size=config.intermediate_size,
            hidden_act=config.hidden_act,
        )

        # PyTorch: self.input_layernorm = LlamaRMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.input_layernorm = TtLlamaRMSNorm(
            self.device,
            state_dict=self.state_dict,
            layer_num = decoder_idx,
            layer_position = 'input_layernorm',
            hidden_size=config.hidden_size,
            eps=config.rms_norm_eps
        )

        # PyTorch: self.post_attention_layernorm = LlamaRMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.post_attention_layernorm = TtLlamaRMSNorm(
            self.device,
            state_dict=self.state_dict,
            layer_num = decoder_idx,
            layer_position = 'post_attention_layernorm',
            hidden_size=config.hidden_size,
            eps=config.rms_norm_eps
        )

    def forward(
        self,
        hidden_states, #: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        output_attentions: Optional[bool] = False,
        use_cache: Optional[bool] = False,
        past_key_value: Optional[Tuple[torch.Tensor]] = None,
    ) -> Tuple[torch.FloatTensor, Optional[Tuple[torch.FloatTensor, torch.FloatTensor]]]:
        """
        Args:
            hidden_states (`torch.FloatTensor`): input to the layer of shape `(batch, seq_len, embed_dim)`
            attention_mask (`torch.FloatTensor`, *optional*): attention mask of size
                `(batch, 1, tgt_len, src_len)` where padding elements are indicated by very large negative values.
            output_attentions (`bool`, *optional*):
                Whether or not to return the attentions tensors of all attention layers. See `attentions` under
                returned tensors for more detail.
            use_cache (`bool`, *optional*):
                If set to `True`, `past_key_values` key value states are returned and can be used to speed up decoding
                (see `past_key_values`).
            past_key_value (`Tuple(torch.FloatTensor)`, *optional*): cached past key and value projection states
        """

        residual = hidden_states

        # PyTorch: hidden_states = self.input_layernorm(hidden_states)
        hidden_states = self.input_layernorm(hidden_states)

        # PyTorch: Self Attention
        # hidden_states, self_attn_weights, present_key_value = self.self_attn(
        #     hidden_states=hidden_states,
        #     past_key_value=past_key_value,
        #     attention_mask=attention_mask,
        #     output_attentions=output_attentions,
        #     use_cache=use_cache,
        # )
        hidden_states, self_attn_weights, present_key_value = self.self_attn(
            hidden_states=hidden_states,
            past_key_value=past_key_value,
            attention_mask=attention_mask,
            output_attentions=output_attentions,
            use_cache=use_cache,
        )

        # Pytorch: hidden_states = residual + hidden_states
        hidden_states = ttl.tensor.add(residual, hidden_states)

        # Fully Connected
        residual = hidden_states

        # Pytorch: hidden_states = self.post_attention_layernorm(hidden_states)
        hidden_states = self.post_attention_layernorm(hidden_states)

        # Pytorch: hidden_states = self.mlp(hidden_states)
        hidden_states = self.mlp(hidden_states)

        # Pytorch: hidden_states = residual + hidden_states
        hidden_states = ttl.tensor.add(residual, hidden_states)

        outputs = (hidden_states,)

        if output_attentions:
            outputs += (self_attn_weights,)

        if use_cache:
            outputs += (present_key_value,)

        # return only hidden_states
        return outputs[0]


class PytorchLlamaDecoderModel(torch.nn.Module):
    def __init__(self, hf_reference_model, decoder_id):
        super().__init__()
        self.decoder = hf_reference_model.model.layers[decoder_id]

        # Disable dropout
        self.decoder.eval()

    def forward(self, x):
        result = self.decoder(x)[0]
        return result


def run_LlamaDecoder_inference():

    tokenizer = AutoTokenizer.from_pretrained("decapoda-research/llama-7b-hf")
    hugging_face_reference_model = AutoModelForCausalLM.from_pretrained("decapoda-research/llama-7b-hf", torch_dtype=torch.float32)
    hugging_face_reference_model.eval()
    configuration = hugging_face_reference_model.config
    state_dict = hugging_face_reference_model.state_dict()

    # Prepare input ========================================================================
    torch.manual_seed(0)
    llama_input = (torch.rand(4, 128, 4096) * 2) - 1
    decoder_id = 31

    # PyTorch output =======================================================================
    pytorch_LlamaDecoder_model = PytorchLlamaDecoderModel(hugging_face_reference_model, decoder_id)
    pytorch_LlamaDecoder_model.eval()
    pytorch_out = pytorch_LlamaDecoder_model(llama_input)

    # TT hardware execution =================================================================
    tt_llama_input = llama_input.unsqueeze(1)
    tt_llama_input = torch2tt_tensor(tt_llama_input, device)

    # get TT Attention module
    tt_LlamaDecoder_model = TtLlamaDecoderLayer(
        device,
        state_dict,
        decoder_id,
        configuration
    )
    tt_out = tt_LlamaDecoder_model(tt_llama_input)
    # transform to PyTorch tensor
    tt_out1 = tt2torch_tensor(tt_out)
    tt_out1= tt_out1.squeeze(1)

    # check outputs ----------------------------------------------------------------------
    print_diff_argmax(pytorch_out, tt_out1)

    # check correlation ceofficient
    print_corr_coef(pytorch_out, tt_out1)

    diff = (abs(pytorch_out - tt_out1) < 0.1).all().item()

    if not diff:
        print("Outputs don't match")
    else:
        print("Outputs match")

    assert diff, "At least one of entries don't match to an absolute difference of 0.1"


if __name__ == "__main__":
    # Initialize the device
    device = ttl.device.CreateDevice(ttl.device.Arch.GRAYSKULL, 0)
    ttl.device.InitializeDevice(device)
    host = ttl.device.GetHost()
    run_LlamaDecoder_inference()
    ttl.device.CloseDevice(device)
