from math import ceil
from random import randrange
import torch
import torch.nn.functional as F
from torch import nn, einsum

from einops.layers.torch import Rearrange, Reduce
from einops import rearrange

# functions

def exists(val):
    return val is not None

def pad_to_multiple(tensor, multiple, dim = -1, value = 0):
    seqlen = tensor.shape[dim]
    m = seqlen / multiple
    if m.is_integer():
        return tensor
    remainder = ceil(m) * multiple - seqlen
    pad_offset = (0,) * (-1 - dim) * 2
    return F.pad(tensor, (*pad_offset, 0, remainder), value = value)

def dropout_layers(layers, prob_survival):
    if prob_survival == 1:
        return layers

    num_layers = len(layers)
    to_drop = torch.zeros(num_layers).uniform_(0., 1.) > prob_survival

    # make sure at least one layer makes it
    if all(to_drop):
        rand_index = randrange(num_layers)
        to_drop[rand_index] = False

    layers = [layer for (layer, drop) in zip(layers, to_drop) if not drop]
    return layers

# helper classes

class Residual(nn.Module):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def forward(self, x):
        return self.fn(x) + x

class PreNorm(nn.Module):
    def __init__(self, dim, fn):
        super().__init__()
        self.fn = fn
        self.norm = nn.LayerNorm(dim)

    def forward(self, x, **kwargs):
        x = self.norm(x)
        return self.fn(x, **kwargs)

class CausalSpatialGatingUnit(nn.Module):
    def __init__(
        self,
        dim,
        dim_seq,
        attn_dim = None,
        init_eps = 1e-3,
        heads = 4
    ):
        super().__init__()
        dim_out = dim // 2

        self.norm = nn.LayerNorm(dim_out)

        self.heads = heads
        self.weight = nn.Parameter(torch.zeros(heads, dim_seq, dim_seq))
        self.bias = nn.Parameter(torch.zeros(heads, dim_seq))

        self.attn = Attention(dim, dim_out, attn_dim) if exists(attn_dim) else None

        init_eps /= dim_seq
        nn.init.uniform_(self.weight, -init_eps, init_eps)
        nn.init.constant_(self.bias, 1.)

    def forward(self, x):
        device, n, h = x.device, x.shape[1], self.heads

        res, gate = x.chunk(2, dim = -1)
        gate = self.norm(gate)

        weight, bias = self.weight, self.bias
        weight, bias = weight[:, :n, :n], bias[:, :n]

        mask = torch.ones(weight.shape[:2], device = device).triu_(1).bool()
        weight = weight.masked_fill(mask[..., None], 0.)

        gate = rearrange(gate, 'b n (h d) -> b h n d', h = h)
        gate = einsum('b h n d, h n m -> b h m d', gate, weight)
        gate = gate + rearrange(bias, 'h n -> () h n ()')
        gate = rearrange(gate, 'b h n d -> b n (h d)')

        return gate * res

def gMLPBlock(
    *,
    dim,
    dim_ff,
    seq_len,
    attn_dim = None,
    heads = 4,
    causal = False
):
    return nn.Sequential(
        nn.Linear(dim, dim_ff),
        nn.GELU(),
        CausalSpatialGatingUnit(dim_ff, seq_len, attn_dim, causal, heads = heads),
        nn.Linear(dim_ff // 2, dim)
    )

# main classes

class gMLPGPT(nn.Module):
    def __init__(
        self,
        *,
        num_tokens = None,
        dim,
        depth,
        seq_len,
        heads = 2,
        ff_mult = 4,
        attn_dim = None,
        prob_survival = 1.,
    ):
        super().__init__()
        dim_ff = dim * ff_mult
        self.seq_len = seq_len
        self.prob_survival = prob_survival

        self.to_embed = nn.Embedding(num_tokens, dim) if exists(num_tokens) else nn.Identity()

        self.layers = nn.ModuleList([Residual(PreNorm(dim, gMLPBlock(dim = dim, dim_ff = dim_ff, seq_len = seq_len, heads = heads, attn_dim = attn_dim))) for i in range(depth)])

        self.to_logits = nn.Sequential(
            nn.LayerNorm(dim),
            nn.Linear(dim, num_tokens)
        ) if exists(num_tokens) else nn.Identity()

    def forward(self, x):
        x = self.to_embed(x)
        layers = self.layers if not self.training else dropout_layers(self.layers, self.prob_survival)
        out = nn.Sequential(*layers)(x)
        return self.to_logits(out)