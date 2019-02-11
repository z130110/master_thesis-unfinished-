#---------------------------------------------------------------------
# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#-------------------------------------------------------------------
### This is the MUSE code of Wasserstain GAN with gradient penalty algorithm,
### some paramaters are added and the adversarial training part is changed by Xuwen Zhang.


import torch
from torch import nn

from .utils import load_embeddings, normalize_embeddings



class Discriminator(nn.Module):

    def __init__(self, params):
        super(Discriminator, self).__init__()

        self.emb_dim = params.emb_dim
        self.dis_layers = params.dis_layers
        self.dis_hid_dim = params.dis_hid_dim
        self.dis_dropout = params.dis_dropout
        self.dis_input_dropout = params.dis_input_dropout
        self.layer_norm = params.layer_norm

        layers = [nn.Dropout(self.dis_input_dropout)]
        for i in range(self.dis_layers):
            input_dim = self.emb_dim if i == 0 else self.dis_hid_dim
            #output_dim = 1 if i == self.dis_layers else self.dis_hid_dim
            output_dim = self.dis_hid_dim
            layers.append(nn.Linear(input_dim, output_dim))
            if i < self.dis_layers:
                if self.layer_norm:
                    layers.append(nn.LayerNorm(output_dim))
                else:
                    pass
                layers.append(nn.LeakyReLU(0.2))
                layers.append(nn.Dropout(self.dis_dropout))
        #layers.append(nn.Sigmoid())
        self.layers = nn.Sequential(*layers)
        self.output = nn.Linear(self.dis_hid_dim,1)


    def forward(self, x):
        assert x.dim() == 2 and x.size(1) == self.emb_dim
        output_ = self.layers(x)
        output = self.output(output_)
        return  output, output_



def build_model(params, with_dis):
    """
    Build all components of the model.
    """
    # source embeddings
    src_dico, _src_emb = load_embeddings(params, source=True)
    #_src_emb = torch.nn.functional.normalize(_src_emb, dim = 1)
    params.src_dico = src_dico
    src_emb = nn.Embedding(len(src_dico), params.emb_dim, sparse=True)
    src_emb.weight.data.copy_(_src_emb)

    # target embeddings
    if params.tgt_lang:
        tgt_dico, _tgt_emb = load_embeddings(params, source=False)
        #_tgt_emb = torch.nn.functional.normalize(_tgt_emb, dim = 1)
        params.tgt_dico = tgt_dico
        tgt_emb = nn.Embedding(len(tgt_dico), params.emb_dim, sparse=True)
        tgt_emb.weight.data.copy_(_tgt_emb)
    else:
        tgt_emb = None

    # mapping
    mapping = nn.Linear(params.emb_dim, params.emb_dim, bias=False)
    if getattr(params, 'map_id_init', True):
        mapping.weight.data.copy_(torch.diag(torch.ones(params.emb_dim)))

    # discriminator
    discriminator = Discriminator(params) if with_dis else None

    # cuda
    if params.cuda:
        src_emb.cuda()
        if params.tgt_lang:
            tgt_emb.cuda()
        mapping.cuda()
        if with_dis:
            discriminator.cuda()

    # normalize embeddings
    params.src_mean = normalize_embeddings(src_emb.weight.data, params.normalize_embeddings)
    if params.tgt_lang:
        params.tgt_mean = normalize_embeddings(tgt_emb.weight.data, params.normalize_embeddings)

    return src_emb, tgt_emb, mapping, discriminator
