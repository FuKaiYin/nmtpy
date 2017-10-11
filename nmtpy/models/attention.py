# -*- coding: utf-8 -*-
from collections import OrderedDict

import numpy as np

import theano
import theano.tensor as tensor

from ..layers import dropout, tanh, get_new_layer
from ..defaults import INT, FLOAT
from ..nmtutils import norm_weight, invert_dictionary, load_dictionary
from ..iterators.text import TextIterator
from ..iterators.bitext import BiTextIterator
from .basemodel import BaseModel

class Model(BaseModel):
    def __init__(self, seed, logger, **kwargs):
        # Call parent's init first
        super(Model, self).__init__(**kwargs)

        # Set logger
        self._logger = logger

        ######################################################
        # All the kwargs arguments come from the configuration
        # file or as extra arguments given to nmt-train.
        ######################################################

        # Use GRU by default as encoder
        # NOTE: not tested at all with LSTM
        self.enc_type = kwargs.pop('enc_type', 'gru')

        # Do we apply layer normalization to GRU encoder?
        # NOTE: layernorm in CGRU seems to degrade the performance
        # so its explicitly disabled.
        self.lnorm = kwargs.pop('layer_norm', False)

        # Shuffle mode (default: trglen (ordered by target len))
        self.smode = kwargs.pop('shuffle_mode', 'trglen')

        # How to initialize CGRU: text (default), zero (initialize with zero)
        self.init_cgru = kwargs.pop('init_cgru', 'text')

        # If enabled, just use the decoder's hidden state for conditioning
        # the target probability instead of dl4mt style 3-way fusion.
        self.simple_output = kwargs.pop('simple_output', False)

        # Get dropout parameters
        self.emb_dropout = kwargs.pop('emb_dropout', 0.)
        self.ctx_dropout = kwargs.pop('ctx_dropout', 0.)
        self.out_dropout = kwargs.pop('out_dropout', 0.)

        # Number of additional GRU encoders for source sentences
        self.n_enc_layers  = kwargs.pop('n_enc_layers' , 1)

        # Shared embedding schemes
        # False: disabled
        # 2way:  Share output embeddings and input embeddings for target
        #        eliminating ff_logit before softmax.
        # 3way:  Share all embeddings in the network including source.
        #        - Prepare single vocab pkl with nmt-build-dict -s option.
        #        - Give the same pkl to both src and trg in model config.
        self.tied_emb = kwargs.pop('tied_emb', False)

        # Let's call source and target embedding layers Wemb_enc and Wemb_dec
        # by default.
        self.src_emb_name = 'Wemb_enc'
        self.trg_emb_name = 'Wemb_dec'

        ###################
        # Load dictionaries
        ###################

        # Pop the dicts first
        dicts = kwargs.pop('dicts')

        if 'src_dict' in kwargs:
            # When translating, the dict will already be available
            self.src_dict = kwargs.pop('src_dict')
            src_idict = invert_dictionary(self.src_dict)
        else:
            # Load them from pkl files
            self.src_dict, src_idict = load_dictionary(dicts['src'])

        if 'trg_dict' in kwargs:
            # When translating, the dict will already be available
            self.trg_dict = kwargs.pop('trg_dict')
            trg_idict = invert_dictionary(self.trg_dict)
        else:
            # Load them from pkl files
            self.trg_dict, trg_idict = load_dictionary(dicts['trg'])

        ####################################################
        # Limit shortlist sizes to replace
        # out-of-shortlist tokens with <unk> in the iterator
        ####################################################
        self.n_words_src = min(self.n_words_src, len(self.src_dict)) \
                if self.n_words_src > 0 else len(self.src_dict)
        self.n_words_trg = min(self.n_words_trg, len(self.trg_dict)) \
                if self.n_words_trg > 0 else len(self.trg_dict)

        # Sanity check for 3-way tying
        if self.tied_emb == '3way':
            # Check that given vocab files are the same
            assert src_dict_file == trg_dict_file, \
                    "Vocabulary files should be the same for 3-way tying."

            assert self.n_words_src == self.n_words_trg, \
                    "Shortlist sizes should be the same for 3-way tying."

            # Use a single name for all embeddings
            self.src_emb_name = self.trg_emb_name = 'Wemb'

        # Set options: the variables until here will be saved
        # to model checkpoints and snapshots as 'opts' dict.
        self.set_options(self.__dict__)

        # No need to store inverted dictionaries so assign them here.
        self.trg_idict = trg_idict
        self.src_idict = src_idict

        # Context dimensionality is 2 times RNN since we use Bi-RNN
        self.ctx_dim = 2 * self.rnn_dim

        # Set the seed of Theano RNG for dropout
        self.set_trng(seed)

        # We call this once to setup dropout mechanism correctly
        self.set_dropout(False)

    @staticmethod
    def beam_search(inputs, f_inits, f_nexts, beam_size=12, maxlen=100, suppress_unks=False, **kwargs):
        # Final results and their scores
        final_sample        = []
        final_score         = []
        final_alignments    = []

        # Initially we have one empty hypothesis with a score of 0
        hyp_alignments  = [[]]
        hyp_samples     = [[]]
        hyp_scores      = np.zeros(1, dtype=FLOAT)

        # Number of models
        n_models        = len(f_inits)

        # Ensembling-aware lists
        next_states     = [None] * n_models
        text_ctxs       = [None] * n_models
        tiled_ctxs      = [None] * n_models
        next_log_ps     = [None] * n_models
        alphas          = [None] * n_models
        aux_ctxs        = [[] for i in range(n_models)]

        for i, f_init in enumerate(f_inits):
            # Get next_state and initial contexts and save them
            # text_ctx: the set of textual annotations
            # aux_ctx: the set of auxiliary (ex: image) annotations
            result = list(f_init(*inputs))
            next_states[i], text_ctxs[i], aux_ctxs[i] = result[0], result[1], result[2:]
            tiled_ctxs[i] = np.tile(text_ctxs[i], [1, 1])

        # Beginning-of-sentence indicator is -1
        next_w = -1 * np.ones((1,), dtype=INT)

        # FIXME: This will break if [0] is not the src sentence, e.g. im2txt models
        maxlen = max(maxlen, inputs[0].shape[0] * 3)

        # Initial beam size
        live_beam = beam_size

        for t in range(maxlen):
            # Get next states
            # In the first iteration, we provide -1 and obtain the log_p's for the
            # first word. In the following iterations tiled_ctx becomes a batch
            # of duplicated left hypotheses. tiled_ctx is always the same except
            # the size of the 2nd dimension as the context vectors of the source
            # sequence is always the same regardless of the decoding process.
            # next_state's shape is (live_beam, rnn_dim)

            # We do this for each model
            for m, f_next in enumerate(f_nexts):
                next_log_ps[m], next_states[m], alphas[m] = f_next(*([next_w, next_states[m], tiled_ctxs[m]] + aux_ctxs[m]))

                if suppress_unks:
                    next_log_ps[m][:, 1] = -np.inf

            # Compute sum of log_p's for the current hypotheses
            cand_scores = hyp_scores[:, None] - sum(next_log_ps)

            # Mean alphas for the mean model (n_models > 1)
            mean_alphas = sum(alphas) / n_models

            # Flatten by modifying .shape (faster)
            cand_scores.shape = cand_scores.size

            # Take the best live_beam hypotheses
            # argpartition makes a partial sort which is faster than argsort
            # (Idea taken from https://github.com/rsennrich/nematus)
            ranks_flat = cand_scores.argpartition(live_beam-1)[:live_beam]

            # Get the costs
            costs = cand_scores[ranks_flat]

            # New states, scores and samples
            live_beam           = 0
            new_hyp_scores      = []
            new_hyp_samples     = []
            new_hyp_alignments  = []

            # This will be the new next states in the next iteration
            hyp_states          = []

            # Find out to which initial hypothesis idx this was belonging
            # Find out the idx of the appended word
            trans_idxs  = ranks_flat // next_log_ps[0].shape[1]
            word_idxs   = ranks_flat % next_log_ps[0].shape[1]

            # Iterate over the hypotheses and add them to new_* lists
            for idx, [ti, wi] in enumerate(zip(trans_idxs, word_idxs)):
                # Form the new hypothesis by appending new word to the left hyp
                new_hyp = hyp_samples[ti] + [wi]
                new_ali = hyp_alignments[ti] + [mean_alphas[ti]]

                if wi == 0:
                    # <eos> found, separate out finished hypotheses
                    final_sample.append(new_hyp)
                    final_score.append(costs[idx])
                    final_alignments.append(new_ali)
                else:
                    # Add formed hypothesis to the new hypotheses list
                    new_hyp_samples.append(new_hyp)
                    # Cumulated cost of this hypothesis
                    new_hyp_scores.append(costs[idx])
                    new_hyp_alignments.append(new_ali)
                    # Hidden state of the decoder for this hypothesis
                    hyp_states.append([next_state[ti] for next_state in next_states])
                    live_beam += 1

            hyp_scores  = np.array(new_hyp_scores, dtype=FLOAT)
            hyp_samples = new_hyp_samples
            hyp_alignments = new_hyp_alignments

            if live_beam == 0:
                break

            # Take the idxs of each hyp's last word
            next_w      = np.array([w[-1] for w in hyp_samples])
            next_states = [np.array(st, dtype=FLOAT) for st in zip(*hyp_states)]
            tiled_ctxs  = [np.tile(ctx, [live_beam, 1]) for ctx in text_ctxs]

        # dump every remaining hypotheses
        for idx in range(live_beam):
            final_sample.append(hyp_samples[idx])
            final_score.append(hyp_scores[idx])
            final_alignments.append(hyp_alignments[idx])

        if not kwargs.get('get_att_alphas', False):
            # Don't send back alignments for nothing
            final_alignments = None

        return final_sample, final_score, final_alignments

    def info(self):
        """Prints some information about the model."""

        self._logger.info('Source vocabulary size: %d', self.n_words_src)
        self._logger.info('Target vocabulary size: %d', self.n_words_trg)
        self._logger.info('%d training samples' % self.train_iterator.n_samples)
        self._logger.info('  %d src UNKs, %d trg UNKs' % (self.train_iterator.n_unks_src, self.train_iterator.n_unks_trg))
        if 'valid_src' in self.data:
            self._logger.info('%d validation samples' % self.valid_iterator.n_samples)
            self._logger.info('  %d src UNKs, %d trg UNKs' % (self.valid_iterator.n_unks_src, self.valid_iterator.n_unks_trg))
        self._logger.info('dropout (emb,ctx,out): %.2f, %.2f, %.2f' % (self.emb_dropout, self.ctx_dropout, self.out_dropout))

    def load_valid_data(self, from_translate=False):
        """Loads validation data."""

        self.valid_ref_files = self.data['valid_trg']
        if isinstance(self.valid_ref_files, str):
            self.valid_ref_files = list([self.valid_ref_files])

        if from_translate:
            self.valid_iterator = TextIterator(
                                    mask=False,
                                    batch_size=1,
                                    file=self.data['valid_src'], dict=self.src_dict,
                                    n_words=self.n_words_src)
        else:
            # Take the first validation item for NLL computation
            self.valid_iterator = BiTextIterator(
                                    batch_size=self.batch_size,
                                    srcfile=self.data['valid_src'], srcdict=self.src_dict,
                                    trgfile=self.valid_ref_files[0], trgdict=self.trg_dict,
                                    n_words_src=self.n_words_src, n_words_trg=self.n_words_trg)

        self.valid_iterator.read()

    def load_data(self):
        """Loads training data and validation data if any."""

        self.train_iterator = BiTextIterator(
                                batch_size=self.batch_size,
                                shuffle_mode=self.smode,
                                logger=self._logger,
                                srcfile=self.data['train_src'], srcdict=self.src_dict,
                                trgfile=self.data['train_trg'], trgdict=self.trg_dict,
                                n_words_src=self.n_words_src,
                                n_words_trg=self.n_words_trg)

        # Prepare batches
        self.train_iterator.read()
        if 'valid_src' in self.data:
            self.load_valid_data()

    ###################################################################
    # The following methods can be redefined in child models inheriting
    # from this basic Attention model.
    ###################################################################
    def init_params(self):
        """Initializes model weights/layers randomly and store them."""

        params = OrderedDict()

        # embedding weights for encoder and decoder
        params[self.src_emb_name] = norm_weight(self.n_words_src, self.embedding_dim, scale=self.weight_init)
        if self.tied_emb != '3way':
            params[self.trg_emb_name] = norm_weight(self.n_words_trg, self.embedding_dim, scale=self.weight_init)

        ############################
        # encoder: bidirectional RNN
        ############################
        # Forward encoder
        params = get_new_layer(self.enc_type)[0](params, prefix='encoder', nin=self.embedding_dim, dim=self.rnn_dim, scale=self.weight_init, layernorm=self.lnorm)
        # Backwards encoder
        params = get_new_layer(self.enc_type)[0](params, prefix='encoder_r', nin=self.embedding_dim, dim=self.rnn_dim, scale=self.weight_init, layernorm=self.lnorm)

        # How many additional encoder layers to stack?
        for i in range(1, self.n_enc_layers):
            params = get_new_layer(self.enc_type)[0](params, prefix='deep_encoder_%d' % i,
                                                     nin=self.ctx_dim, dim=self.ctx_dim,
                                                     scale=self.weight_init, layernorm=self.lnorm)

        ############################
        # How do we initialize CGRU?
        ############################
        if self.init_cgru == 'text':
            # init_state computation from mean textual context
            params = get_new_layer('ff')[0](params, prefix='ff_state', nin=self.ctx_dim, nout=self.rnn_dim, scale=self.weight_init)

        #########
        # decoder
        #########
        params = get_new_layer('gru_cond')[0](params, prefix='decoder', nin=self.embedding_dim, dim=self.rnn_dim, dimctx=self.ctx_dim, scale=self.weight_init, layernorm=False)

        ########
        # fusion
        ########
        params = get_new_layer('ff')[0](params, prefix='ff_logit_gru'  , nin=self.rnn_dim       , nout=self.embedding_dim, scale=self.weight_init, ortho=False)
        if not self.simple_output:
            params = get_new_layer('ff')[0](params, prefix='ff_logit_prev' , nin=self.embedding_dim , nout=self.embedding_dim, scale=self.weight_init, ortho=False)
            params = get_new_layer('ff')[0](params, prefix='ff_logit_ctx'  , nin=self.ctx_dim       , nout=self.embedding_dim, scale=self.weight_init, ortho=False)
        if self.tied_emb is False:
            params = get_new_layer('ff')[0](params, prefix='ff_logit'  , nin=self.embedding_dim , nout=self.n_words_trg, scale=self.weight_init)

        self.initial_params = params

    def build(self):
        """Builds the computation graph for training."""

        # description string: #words x #samples
        x = tensor.matrix('x', dtype=INT)
        x_mask = tensor.matrix('x_mask', dtype=FLOAT)
        y = tensor.matrix('y', dtype=INT)
        y_mask = tensor.matrix('y_mask', dtype=FLOAT)

        self.inputs = OrderedDict()
        self.inputs['x'] = x
        self.inputs['x_mask'] = x_mask
        self.inputs['y'] = y
        self.inputs['y_mask'] = y_mask

        # for the backward rnn, we just need to invert x and x_mask
        xr = x[::-1]
        xr_mask = x_mask[::-1]

        n_timesteps = x.shape[0]
        n_timesteps_trg = y.shape[0]
        n_samples = x.shape[1]

        # word embedding for forward rnn (source)
        emb = dropout(self.tparams[self.src_emb_name][x.flatten()],
                      self.trng, self.emb_dropout, self.use_dropout)
        emb = emb.reshape([n_timesteps, n_samples, self.embedding_dim])
        proj = get_new_layer(self.enc_type)[1](self.tparams, emb, prefix='encoder', mask=x_mask, layernorm=self.lnorm)

        # word embedding for backward rnn (source)
        embr = dropout(self.tparams[self.src_emb_name][xr.flatten()],
                       self.trng, self.emb_dropout, self.use_dropout)
        embr = embr.reshape([n_timesteps, n_samples, self.embedding_dim])
        projr = get_new_layer(self.enc_type)[1](self.tparams, embr, prefix='encoder_r', mask=xr_mask, layernorm=self.lnorm)

        # context will be the concatenation of forward and backward rnns
        ctx = [tensor.concatenate([proj[0], projr[0][::-1]], axis=proj[0].ndim-1)]

        for i in range(1, self.n_enc_layers):
            ctx = get_new_layer(self.enc_type)[1](self.tparams, ctx[0],
                                                  prefix='deepencoder_%d' % i,
                                                  mask=x_mask, layernorm=self.lnorm)

        # Apply dropout
        ctx = dropout(ctx[0], self.trng, self.ctx_dropout, self.use_dropout)

        if self.init_cgru == 'text':
            # mean of the context (across time) will be used to initialize decoder rnn
            ctx_mean   = (ctx * x_mask[:, :, None]).sum(0) / x_mask.sum(0)[:, None]
            init_state = get_new_layer('ff')[1](self.tparams, ctx_mean, prefix='ff_state', activ='tanh')
        else:
            # Assume zero-initialized decoder
            init_state = tensor.alloc(0., n_samples, self.rnn_dim)

        # word embedding (target), we will shift the target sequence one time step
        # to the right. This is done because of the bi-gram connections in the
        # readout and decoder rnn. The first target will be all zeros and we will
        # not condition on the last output.
        emb = self.tparams[self.trg_emb_name][y.flatten()]
        emb = emb.reshape([n_timesteps_trg, n_samples, self.embedding_dim])
        emb_shifted = tensor.zeros_like(emb)
        emb_shifted = tensor.set_subtensor(emb_shifted[1:], emb[:-1])
        emb = emb_shifted

        # decoder - pass through the decoder conditional gru with attention
        r = get_new_layer('gru_cond')[1](self.tparams, emb,
                                         prefix='decoder',
                                         mask=y_mask, context=ctx,
                                         context_mask=x_mask,
                                         one_step=False,
                                         init_state=init_state, layernorm=False)
        # hidden states of the decoder gru
        # weighted averages of context, generated by attention module
        # weights (alignment matrix)
        next_state, ctxs, alphas = r

        # compute word probabilities
        logit = get_new_layer('ff')[1](self.tparams, next_state, prefix='ff_logit_gru', activ='linear')

        if not self.simple_output:
            logit += get_new_layer('ff')[1](self.tparams, ctxs, prefix='ff_logit_ctx', activ='linear')
            logit += get_new_layer('ff')[1](self.tparams, emb, prefix='ff_logit_prev', activ='linear')

        logit = dropout(tanh(logit), self.trng, self.out_dropout, self.use_dropout)

        if self.tied_emb is False:
            logit = get_new_layer('ff')[1](self.tparams, logit, prefix='ff_logit', activ='linear')
        else:
            logit = tensor.dot(logit, self.tparams[self.trg_emb_name].T)

        logit_shp = logit.shape

        # Apply logsoftmax (stable version)
        log_probs = -tensor.nnet.logsoftmax(logit.reshape([logit_shp[0]*logit_shp[1], logit_shp[2]]))

        # cost
        y_flat = y.flatten()
        y_flat_idx = tensor.arange(y_flat.shape[0]) * self.n_words_trg + y_flat

        cost = log_probs.flatten()[y_flat_idx]
        cost = cost.reshape([n_timesteps_trg, n_samples])
        cost = (cost * y_mask).sum(0)

        self.f_log_probs = theano.function(list(self.inputs.values()), cost)

        return cost

    def build_sampler(self):
        """Builds the computation graph for beam search."""

        x           = tensor.matrix('x', dtype=INT)
        xr          = x[::-1]
        n_timesteps = x.shape[0]
        n_samples   = x.shape[1]

        # word embedding (source), forward and backward
        emb = self.tparams[self.src_emb_name][x.flatten()]
        emb = emb.reshape([n_timesteps, n_samples, self.embedding_dim])

        embr = self.tparams[self.src_emb_name][xr.flatten()]
        embr = embr.reshape([n_timesteps, n_samples, self.embedding_dim])

        # encoder
        proj  = get_new_layer(self.enc_type)[1](self.tparams, emb, prefix='encoder', layernorm=self.lnorm)
        projr = get_new_layer(self.enc_type)[1](self.tparams, embr, prefix='encoder_r', layernorm=self.lnorm)

        # concatenate forward and backward rnn hidden states
        ctx = [tensor.concatenate([proj[0], projr[0][::-1]], axis=proj[0].ndim-1)]

        for i in range(1, self.n_enc_layers):
            ctx = get_new_layer(self.enc_type)[1](self.tparams, ctx[0],
                                                  prefix='deepencoder_%d' % i,
                                                  layernorm=self.lnorm)

        ctx = ctx[0]

        if self.init_cgru == 'text' and 'ff_state_W' in self.tparams:
            # get the input for decoder rnn initializer mlp
            ctx_mean = ctx.mean(0)
            init_state = get_new_layer('ff')[1](self.tparams, ctx_mean, prefix='ff_state', activ='tanh')
        else:
            # assume zero-initialized decoder
            init_state = tensor.alloc(0., n_samples, self.rnn_dim)

        outs = [init_state, ctx]
        self.f_init = theano.function([x], outs, name='f_init')

        # x: 1 x 1
        y = tensor.vector('y_sampler', dtype=INT)
        init_state = tensor.matrix('init_state', dtype=FLOAT)

        # if it's the first word, emb should be all zero and it is indicated by -1
        emb = tensor.switch(y[:, None] < 0,
                            tensor.alloc(0., 1, self.tparams[self.trg_emb_name].shape[1]),
                            self.tparams[self.trg_emb_name][y])

        # apply one step of conditional gru with attention
        # get the next hidden states
        # get the weighted averages of contexts for this target word y
        r = get_new_layer('gru_cond')[1](self.tparams, emb,
                                         prefix='decoder',
                                         mask=None, context=ctx,
                                         one_step=True,
                                         init_state=init_state, layernorm=False)

        next_state, ctxs, alphas = r

        logit = get_new_layer('ff')[1](self.tparams, next_state, prefix='ff_logit_gru', activ='linear')

        if not self.simple_output:
            logit += get_new_layer('ff')[1](self.tparams, emb, prefix='ff_logit_prev',activ='linear')
            logit += get_new_layer('ff')[1](self.tparams, ctxs, prefix='ff_logit_ctx', activ='linear')

        logit = tanh(logit)

        if self.tied_emb is False:
            logit = get_new_layer('ff')[1](self.tparams, logit, prefix='ff_logit', activ='linear')
        else:
            logit = tensor.dot(logit, self.tparams[self.trg_emb_name].T)

        # compute the logsoftmax
        next_log_probs = tensor.nnet.logsoftmax(logit)

        # compile a function to do the whole thing above
        # next hidden state to be used
        inputs = [y, init_state, ctx]

        outs = [next_log_probs, next_state, alphas]
        self.f_next = theano.function(inputs, outs, name='f_next')
