from six.moves import range
from six.moves import zip

# Python
import os
import cPickle
import inspect
import importlib

from collections import OrderedDict

# 3rd party
import numpy as np

import theano
import theano.tensor as tensor

# Ours
from ..layers import *
from ..typedef import *
from ..nmtutils import *
from ..iterators.wmt import WMTIterator

from ..models.attention import Model as ParentModel

# Same model as attention but using WMTIterator
class Model(ParentModel):
    def __init__(self, seed, logger, **kwargs):
        # Call parent's init first
        super(Model, self).__init__(seed, logger, **kwargs)

        self.data_mode = kwargs.get('data_mode', 'pairs')

    def load_valid_data(self, from_translate=False, data_mode='all'):
        if from_translate:
            self.valid_ref_files = self.data['valid_trg']
            if isinstance(self.valid_ref_files, str):
                self.valid_ref_files = list([self.valid_ref_files])

            self.valid_iterator = WMTIterator(
                    batch_size=1,
                    mask=False,
                    pklfile=self.data['valid_src'],
                    srcdict=self.src_dict, n_words_src=self.n_words_src,
                    mode='single') # Override the given parameter
        else:
            # Take the first validation item for NLL computation
            self.valid_iterator = WMTIterator(
                    batch_size=self.batch_size,
                    pklfile=self.data['valid_src'],
                    trgdict=self.trg_dict, srcdict=self.src_dict,
                    n_words_trg=self.n_words_trg, n_words_src=self.n_words_src,
                    mode='single') # Override the given parameter

        self.valid_iterator.read()

    def load_data(self):
        self.train_iterator = WMTIterator(
                batch_size=self.batch_size,
                shuffle_mode=self.smode,
                logger=self.logger,
                pklfile=self.data['train_src'],
                trgdict=self.trg_dict, srcdict=self.src_dict,
                n_words_trg=self.n_words_trg, n_words_src=self.n_words_src,
                mode=self.data_mode)
        # Prepare batches
        self.train_iterator.read()
        self.load_valid_data()
