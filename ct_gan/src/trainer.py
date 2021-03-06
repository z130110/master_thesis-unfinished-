#----------------------------------------------------------------------------
# Copyright (c) 2017-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#----------------------------------------------------------------------------
### This is the MUSE code of Wasserstain GAN with gradient penalty algorithm,
### some paramaters are added and the adversarial training part is changed by Xuwen Zhang.


import os
from logging import getLogger
import scipy
import scipy.linalg
import torch
from torch.autograd import Variable
from torch.nn import functional as F

from .utils import get_optimizer, load_embeddings, normalize_embeddings, export_embeddings
from .utils import clip_parameters
from .dico_builder import build_dictionary
from .evaluation.word_translation import DIC_EVAL_PATH, load_identical_char_dico, load_dictionary


logger = getLogger()


class Trainer(object):

    def __init__(self, src_emb, tgt_emb, mapping, discriminator, params):
        """
        Initialize trainer script.
        """
        self.src_emb = src_emb
        self.tgt_emb = tgt_emb
        self.src_dico = params.src_dico
        self.tgt_dico = getattr(params, 'tgt_dico', None)
        self.mapping = mapping
        self.discriminator = discriminator
        self.params = params
        self.one_1 = Variable(torch.FloatTensor([1]).cuda())
        self.mone_1 = self.one_1 * -1 
        self.mone_1 = Variable(self.mone_1.cuda())

        # optimizers
        if hasattr(params, 'map_optimizer'):
            optim_fn, optim_params = get_optimizer(params.map_optimizer)
            self.map_optimizer = optim_fn(mapping.parameters(), **optim_params)
        if hasattr(params, 'dis_optimizer'):
            optim_fn, optim_params = get_optimizer(params.dis_optimizer)
            self.dis_optimizer = optim_fn(discriminator.parameters(), **optim_params)
        else:
            assert discriminator is None

        # best validation score
        self.best_valid_metric = -1e12
        self.decrease_lr = False

        # define labels
        self.label_real = torch.FloatTensor(self.params.batch_size).zero_() + self.params.dis_smooth
        self.label_fake = torch.FloatTensor(self.params.batch_size).zero_() + (1 - self.params.dis_smooth)
        self.label_real = Variable(self.label_real.cuda() if self.params.cuda else self.label_real)
        self.label_fake = Variable(self.label_fake.cuda() if self.params.cuda else self.label_fake)


    def get_dis_xy(self, req_grad):
        """
        Get discriminator input batch / output target.
        """
        # select random word IDs
        bs = self.params.batch_size
        mf = self.params.dis_most_frequent
        assert mf <= min(len(self.src_dico), len(self.tgt_dico))
        src_ids = torch.LongTensor(bs).random_(len(self.src_dico) if mf == 0 else mf)
        tgt_ids = torch.LongTensor(bs).random_(len(self.tgt_dico) if mf == 0 else mf)
        if self.params.cuda:
            src_ids = src_ids.cuda()
            tgt_ids = tgt_ids.cuda()

        # get word embeddings
        src_emb = self.src_emb(Variable(src_ids, requires_grad = False))
        tgt_emb = self.tgt_emb(Variable(tgt_ids, requires_grad = False))
        src_emb = self.mapping(Variable(src_emb.data.cuda() if self.params.cuda else src_emb.data, requires_grad = req_grad))
        tgt_emb = Variable(tgt_emb.data.cuda() if self.params.cuda else tgt_emb.data, requires_grad = True)
        return src_emb, tgt_emb

    def consistency_term(self, real_data):
        d1, d_1 = self.discriminator(Variable(real_data))
        d2, d_2 = self.discriminator(Variable(real_data))
        # why max is needed when norm is positive?
        consistency_term = (d1 - d2).norm(2, dim=1) + 0.1 * \
            (d_1 - d_2).norm(2, dim=1) - self.params.ct_m
        consistency_term = consistency_term.mean(0).view(1)
        consistency_term = torch.max(torch.FloatTensor([0]).cuda(), consistency_term).view(1)
        return consistency_term.mean(0).view(1) * self.params.ct_lambda

    def dis_step(self, stats):
        """
        Train the discriminator.
        """
        self.discriminator.train()

        batch_s = self.params.batch_size

        src_emb, tgt_emb = self.get_dis_xy(req_grad = False)

        output_fake = self.discriminator(Variable(src_emb.data))
        output_fake = output_fake[0].mean(0).view(1)
        output_real = self.discriminator(Variable(tgt_emb.data))
        output_real = output_real[0].mean(0).view(1)

       
        # gradient panelty term
        alpha_ = torch.rand(batch_s, 1)
        alpha_ = alpha_.expand(tgt_emb.size())
        alpha_ = alpha_.cuda() if self.params.cuda else alpha
        
        interpolates = alpha_ * tgt_emb + ((1 - alpha_) * src_emb)
        if self.params.cuda:
            interpolates = interpolates.cuda()
        interpolates = Variable(interpolates, requires_grad=True)
        disc_interpolates = self.discriminator(interpolates)
        disc_interpolates = disc_interpolates[0].mean(0).view(1)
        gradient_D_interpolates = torch.autograd.grad(outputs=disc_interpolates, inputs=interpolates, \
                                  grad_outputs=torch.ones(disc_interpolates.size()).cuda() \
                                  if self.params.cuda else torch.ones(disc_interpolates.size()), \
                                  create_graph=True, retain_graph=True, only_inputs=True)[0]

        gradient_penalty = ((gradient_D_interpolates.norm(2, dim=1) - 1) ** 2).mean() * self.params.grad_lambda        
        gradient_penalty = gradient_penalty.view(1).cuda()       

        c_t = self.consistency_term(tgt_emb)

        loss = output_fake - output_real + gradient_penalty + c_t

        stats['DIS_COSTS'].append(loss.data[0])
        # stats["grad_penalty"].append(gradient_penalty.data[0])
        # stats["c_term"].append(c_t.data[0])
            
        # check NaN
        if (loss != loss).data.any():
            logger.error("NaN detected (discriminator)")
            exit()
  
        
        self.discriminator.zero_grad()  
        loss.backward() 
        self.dis_optimizer.step()   


    def mapping_step(self, stats):
        """
        Fooling discriminator training step.
        """
        if self.params.dis_lambda == 0:
            return 0

        self.discriminator.eval()
        batch_s = self.params.batch_size        

        # loss
        src_emb, tgt_emb = self.get_dis_xy(req_grad = True)
 
        output_fake = self.discriminator(src_emb)
        output_fake = output_fake[0].mean(0).view(1)
        loss = -output_fake

        #loss = self.params.dis_lambda * loss

        stats["Generator_Cost"].append(loss.data[0])


        # check NaN
        if (loss != loss).data.any():
            logger.error("NaN detected (fool discriminator)")
            exit()

        # optim
        self.mapping.zero_grad()
        loss.backward()
        self.map_optimizer.step()
        self.orthogonalize()
        return 2*self.params.batch_size


    def load_training_dico(self, dico_train):
        """
        Load training dictionary.
        """
        word2id1 = self.src_dico.word2id
        word2id2 = self.tgt_dico.word2id

        # identical character strings
        if dico_train == "identical_char":
            self.dico = load_identical_char_dico(word2id1, word2id2)
        # use one of the provided dictionary
        elif dico_train == "default":
            filename = '%s-%s.0-5000.txt' % (self.params.src_lang, self.params.tgt_lang)
            self.dico = load_dictionary(
                os.path.join(DIC_EVAL_PATH, filename),
                word2id1, word2id2
            )
        # dictionary provided by the user
        else:
            self.dico = load_dictionary(dico_train, word2id1, word2id2)

        # cuda
        if self.params.cuda:
            self.dico = self.dico.cuda()

    def build_dictionary(self):
        """
        Build a dictionary from aligned embeddings.
        """
        src_emb = self.mapping(self.src_emb.weight).data
        tgt_emb = self.tgt_emb.weight.data
        src_emb = src_emb / src_emb.norm(2, 1, keepdim=True).expand_as(src_emb)
        tgt_emb = tgt_emb / tgt_emb.norm(2, 1, keepdim=True).expand_as(tgt_emb)
        self.dico = build_dictionary(src_emb, tgt_emb, self.params)
        #print("-----------------------------")
        #print("finished trainer.build_dictionary")

    def procrustes(self):
        """
        Find the best orthogonal matrix mapping using the Orthogonal Procrustes problem
        https://en.wikipedia.org/wiki/Orthogonal_Procrustes_problem
        """
        #print("<<<<<<<<<<<<<>>>>>>>>>>>>>>>>")
        #print("This is self.dico")
        #print(self.dico)
        #print("<<<<<<<<<<>>>>>>>>>>>>>>>>>>>")
        #print(self.src_emb.weight.data)
        A = self.src_emb.weight.data[self.dico[:, 0]]
        B = self.tgt_emb.weight.data[self.dico[:, 1]]
        W = self.mapping.weight.data
        M = B.transpose(0, 1).mm(A).cpu().numpy()
        U, S, V_t = scipy.linalg.svd(M, full_matrices=True)
        W.copy_(torch.from_numpy(U.dot(V_t)).type_as(W))

    def orthogonalize(self):
        """
        Orthogonalize the mapping.
        """
        if self.params.map_beta > 0:
            W = self.mapping.weight.data
            beta = self.params.map_beta
            W.copy_((1 + beta) * W - beta * W.mm(W.transpose(0, 1).mm(W)))

    def update_lr(self, to_log, metric):
        """
        Update learning rate when using SGD.
        """
        #if 'sgd' not in self.params.map_optimizer:
        #    return

        old_lr = self.map_optimizer.param_groups[0]['lr']
        new_lr = max(self.params.min_lr, old_lr * self.params.lr_decay)
        if new_lr < old_lr:
            logger.info("Decreasing learning rate: %.8f -> %.8f" % (old_lr, new_lr))
            self.map_optimizer.param_groups[0]['lr'] = new_lr

        if self.params.lr_shrink < 1 and to_log[metric] >= -1e7:
            if to_log[metric] < self.best_valid_metric:
                logger.info("Validation metric is smaller than the best: %.5f vs %.5f"
                            % (to_log[metric], self.best_valid_metric))
                # decrease the learning rate, only if this is the
                # second time the validation metric decreases
                if self.decrease_lr:
                    old_lr = self.map_optimizer.param_groups[0]['lr']
                    self.map_optimizer.param_groups[0]['lr'] *= self.params.lr_shrink
                    logger.info("Shrinking the learning rate: %.5f -> %.5f"
                                % (old_lr, self.map_optimizer.param_groups[0]['lr']))
                self.decrease_lr = True

    def save_best(self, to_log, metric):
        """
        Save the best model for the given validation metric.
        """
        # best mapping for the given validation criterion
        if to_log[metric] > self.best_valid_metric:
            # new best mapping
            self.best_valid_metric = to_log[metric]
            logger.info('* Best value for "%s": %.5f' % (metric, to_log[metric]))
            # save the mapping
            W = self.mapping.weight.data.cpu().numpy()
            path = os.path.join(self.params.exp_path, 'best_mapping.pth')
            logger.info('* Saving the mapping to %s ...' % path)
            torch.save(W, path)

    def reload_best(self):
        """
        Reload the best mapping.
        """
        path = os.path.join(self.params.exp_path, 'best_mapping.pth')
        logger.info('* Reloading the best model from %s ...' % path)
        # reload the model
        assert os.path.isfile(path)
        to_reload = torch.from_numpy(torch.load(path))
        W = self.mapping.weight.data
        assert to_reload.size() == W.size()
        W.copy_(to_reload.type_as(W))

    def export(self):
        """
        Export embeddings.
        """
        params = self.params

        # load all embeddings
        logger.info("Reloading all embeddings for mapping ...")
        params.src_dico, src_emb = load_embeddings(params, source=True, full_vocab=True)
        params.tgt_dico, tgt_emb = load_embeddings(params, source=False, full_vocab=True)

        # apply same normalization as during training
        normalize_embeddings(src_emb, params.normalize_embeddings, mean=params.src_mean)
        normalize_embeddings(tgt_emb, params.normalize_embeddings, mean=params.tgt_mean)

        # map source embeddings to the target space
        bs = 4096
        logger.info("Map source embeddings to the target space ...")
        for i, k in enumerate(range(0, len(src_emb), bs)):
            x = Variable(src_emb[k:k + bs], volatile=True)
            src_emb[k:k + bs] = self.mapping(x.cuda() if params.cuda else x).data.cpu()

        # write embeddings to the disk
        export_embeddings(src_emb, tgt_emb, params)
