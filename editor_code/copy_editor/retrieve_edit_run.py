# import cPickle as pickle
import pickle
import codecs
import random
from collections import defaultdict
# from itertools import izip
from itertools import zip_longest as izip # Alex
from os.path import dirname, realpath, join

import numpy as np
import torch.optim as optim
import unicodedata
import sys

from editor_code.copy_editor import data
from editor_code.copy_editor.attention_decoder import AttentionDecoderCell
from editor_code.copy_editor.editor import EditExample, Editor
from editor_code.copy_editor.vae_editret import VAERetriever
from editor_code.copy_editor.encoder import Encoder
from editor_code.copy_editor.vocab import load_embeddings
from editor_code.copy_editor.utils import edit_dist
from gtd.chrono import verboserate
from gtd.io import num_lines, makedirs
from gtd.ml.torch.token_embedder import TokenEmbedder
from gtd.ml.torch.training_run import TorchTrainingRun
from gtd.ml.torch.utils import similar_size_batches, try_gpu, random_seed, random_state
from gtd.ml.training_run import TrainingRuns
from gtd.ml.training_run_viewer import TrainingRunViewer, Commit, JSONSelector, NumSteps, run_name, Owner
from gtd.utils import sample_if_large, bleu, Config, chunks

from edit_training_run import EditDataSplits
from editor_code.copy_editor.edit_retriever import EditRetriever

class RetrieveEditTrainingRuns(TrainingRuns):
    def __init__(self, check_commit=True):
        data_dir = data.workspace.edit_runs
        src_dir = dirname(dirname(realpath(__file__)))  # root of the Git repo
        super(RetrieveEditTrainingRuns, self).__init__(data_dir, src_dir, RetrieveEditTrainingRun, check_commit=check_commit)


class RetrieveEditTrainingRunsViewer(TrainingRunViewer):
    def __init__(self):
        runs = RetrieveEditTrainingRuns(check_commit=False)
        super(RetrieveEditTrainingRunsViewer, self).__init__(runs)

        meta = lambda paths: JSONSelector('metadata.txt', paths)
        two_digits = lambda x: round(x, 2)

        # list different formats in which stats were logged
        legacy = lambda name, high_or_low: [
            ('{}_valid'.format(name),),
            ('stats', 'big', name, 'valid'),
            ('stats', 'big', name, 'valid', high_or_low),
            ('stats', 'big', name, high_or_low, 'valid'),
        ]

        self.add('#', run_name)
        self.add('owner', Owner({}))
        self.add('dset', meta(('config', 'dataset', 'path')))
        self.add('enc dropout', meta(('config', 'model', 'encoder_dropout_prob')))
        self.add('dec dropout', meta(('config', 'model', 'decoder_dropout_prob')))
        self.add('steps', NumSteps())

        # best scores on the big validation set
        self.add('bleu', meta(legacy('bleu', 'high') + legacy('avg_bleu', 'high')), two_digits)
        self.add('edit dist', meta(legacy('edit_dist', 'low')), two_digits)
        self.add('exact match', meta(legacy('exact_match', 'high') + legacy('exact_match_prob', 'high')), two_digits)
        self.add('loss', meta(legacy('loss', 'low')), two_digits)

        self.add('last seen', meta(('last_seen',)))
        self.add('commit', Commit())



class RetrieveEditTrainingRun(TorchTrainingRun):
    def __init__(self, config, save_dir, ckpt=None):
        super(RetrieveEditTrainingRun, self).__init__(config, save_dir)

        # extra dir for storing TrainStates where NaN was encountered
        self.workspace.add_dir('nan_checkpoints')
        self.workspace.add_dir('traces')

        # build model
        with random_seed(config.optim.seed):
            print('seed:'+str(config.optim.seed))
            model, optimizer = self._build_model(config.model, config.optim, config.dataset)
            if ckpt is None:
                self.train_state = self.checkpoints.load_latest(model, optimizer)
            else:
                self.train_state = self.checkpoints.load(ckpt, model, optimizer)

        # load data
        data_dir = join(data.workspace.datasets, config.dataset.path)
        self._examples = EditDataSplits(data_dir, config.dataset)


    @property
    def editor(self):
        """Return the Editor."""
        return self.train_state.model

    @property
    def examples(self):
        """Return data splits (EditDataSplits)."""
        return self._examples

    @classmethod
    def _build_editor(cls, model_config, data_config, word_embeddings, word_dim, vae_mode):
        source_token_embedder = TokenEmbedder(word_embeddings, model_config.train_source_embeds)
        target_token_embedder = TokenEmbedder(word_embeddings, model_config.train_target_embeds)

        # number of input channels
        if vae_mode:
            num_inputs = len(data_config.source_cols)
        else: #edit model uses num_inputs + num_inputs + 1
            num_inputs = len(data_config.source_cols)*2+1

        decoder_cell = AttentionDecoderCell(target_token_embedder, 2 * word_dim,
                                            # 2 * word_dim because we concat base and copy vectors
                                            model_config.agenda_dim, model_config.hidden_dim, model_config.hidden_dim,
                                            model_config.attention_dim,
                                            num_layers=model_config.decoder_layers,
                                            num_inputs=num_inputs, dropout_prob=model_config.decoder_dropout_prob,
                                            disable_attention=vae_mode)

        if vae_mode:
            encoder = Encoder(word_dim, model_config.agenda_dim, model_config.hidden_dim,
                              model_config.encoder_layers, num_inputs, model_config.encoder_dropout_prob, vae_mode,
                              model_config.vae_kappa)
        else:
            encoder = Encoder(word_dim, model_config.agenda_dim, model_config.hidden_dim,
                              model_config.encoder_layers, num_inputs, model_config.encoder_dropout_prob, vae_mode)

        vae_copy_len = [5,10,185]
        editor_copy_len = [5,10,10,5,10,10,150]
        if vae_mode:
            model = Editor(source_token_embedder, encoder, decoder_cell, vae_copy_len)
        else:
            model = Editor(source_token_embedder, encoder, decoder_cell, editor_copy_len)
        model = try_gpu(model)
        return model

    @classmethod
    def _build_model(cls, model_config, optim_config, data_config):
        """Build Editor.

        Args:
            model_config (Config): Editor config
            optim_config (Config): optimization config
            data_config (Config): dataset config

        Returns:
            Editor
        """

        file_path = join(data.workspace.word_vectors, model_config.wvec_path)
        word_embeddings = load_embeddings(file_path, model_config.word_dim,
                                          model_config.vocab_size,model_config.num_copy_tokens)
        word_dim = word_embeddings.embed_dim

        edit_model = cls._build_editor(model_config, data_config, word_embeddings, word_dim, vae_mode=False)

        #VAEretreiver
        vocab_dict = word_embeddings.vocab._word2index
        encoder = Encoder(word_dim, model_config.agenda_dim, model_config.hidden_dim,
                          model_config.encoder_layers, len(data_config.source_cols), model_config.encoder_dropout_prob, use_vae = True, kappa = model_config.vae_kappa, use_target=False)
        source_token_embedder = TokenEmbedder(word_embeddings, model_config.train_source_embeds)
        target_token_embedder = TokenEmbedder(word_embeddings, model_config.train_target_embeds)
        ret_copy_len = [5, 10, 165]
        num_inputs = len(data_config.source_cols)
        decoder_cell = AttentionDecoderCell(target_token_embedder, 2 * word_dim,
                                            # 2 * word_dim because we concat base and copy vectors
                                            model_config.agenda_dim, model_config.hidden_dim, model_config.hidden_dim,
                                            model_config.attention_dim,
                                            num_layers=model_config.decoder_layers,
                                            num_inputs=num_inputs, dropout_prob=model_config.decoder_dropout_prob,
                                            disable_attention=True)
        vae_model = VAERetriever(source_token_embedder, encoder, decoder_cell, ret_copy_len)
        ret_model = vae_model
        
        vae_ret_model = EditRetriever(vae_model, ret_model, edit_model)
        vae_ret_model = try_gpu(vae_ret_model)

        optimizer = optim.Adam(vae_ret_model.parameters(), lr=optim_config.learning_rate)
        #optimizer = optim.SGD(vae_ret_model.parameters(), lr=optim_config.learning_rate)

        return vae_ret_model, optimizer

    def train_vae(self):
        # test batching!
        train_state = self.train_state
        examples = self._examples
        config = self.config
        workspace = self.workspace

        vae_editor = train_state.model.vae_model
        ret_model = train_state.model.ret_model
        edit_model = train_state.model.edit_model
        train_batches = similar_size_batches(examples.train, config.optim.batch_size)

        vae_editor.test_batch(train_batches[0])

        step = 0
        while step < config.optim.max_iters:
            random.shuffle(train_batches)
            for batch in verboserate(train_batches, desc='Streaming training examples'):
                loss, _, _ = vae_editor.loss(batch)
                finite_grads, grad_norm = self._take_grad_step(train_state, loss)
                self.check_gradnan(finite_grads, train_state, workspace)
                step = train_state.train_steps
                self.eval_and_save(vae_editor, step, train_state, config, grad_norm, examples.train, examples.valid)
                if step >= config.optim.max_iters:
                    break

    def setup_ret(self):
        """ use the existing ret model encoder """ 
        # TODO(kelvin): do something to preserve random state upon reload?
        train_state = self.train_state
        examples = self._examples
        ret_model = train_state.model.ret_model
        new_vecs = ret_model.batch_embed(examples.train)
        new_lsh = ret_model.make_lsh(new_vecs)
        return new_lsh

    
    def train_edit(self, use_lsh, topk):
        # TODO(kelvin): do something to preserve random state upon reload?
        train_state = self.train_state
        examples = self._examples
        config = self.config
        workspace = self.workspace

        vae_editor = train_state.model.vae_model
        ret_model = train_state.model.ret_model
        edit_model = train_state.model.edit_model

        # Set up static editor training
        step = train_state.train_steps
        while step < 3 * config.optim.max_iters:
            train_eval = ret_model.ret_and_make_ex(examples.train, use_lsh, examples.train, 1)
            valid_eval = ret_model.ret_and_make_ex(examples.valid, use_lsh, examples.train, 0)
            ret_batches = similar_size_batches(train_eval, config.optim.batch_size)
            # random.shuffle(train_batches)
            random.shuffle(ret_batches)
            for batch in verboserate(ret_batches, desc='Streaming training for retrieval'):
                # Set up pairs to edit on
                fict_batch = edit_model.ident_mapper(batch, config.model.ident_pr)
                edit_loss, _, _ = edit_model.loss(fict_batch)
                loss = edit_loss
                finite_grads, grad_norm = self._take_grad_step(train_state, loss)
                self.check_gradnan(finite_grads, train_state, workspace)
                step = train_state.train_steps
                self.eval_and_save(edit_model, step, train_state, config, grad_norm, train_eval, valid_eval)

                if step >= 3 * config.optim.max_iters:
                    break
                pass
            # TODO: Proper eval code for retrieval step.

    def train(self):
        """Train a model.

        NOTE: modifies TrainState in place.
        - parameters of the Editor and Optimizer are updated
        - train_steps is updated
        - random number generator states are updated at every checkpoint
        """
        with random_state(self.train_state.random_state):
            self.train_vae()
            lsh = self.setup_ret()
            self.lsh = lsh
            self.train_edit(lsh,1)


    def check_gradnan(self, finite_grads, train_state, workspace):
        # somehow we encountered NaN
        if not finite_grads:
            # dump parameters
            #train_state.save(workspace.nan_checkpoints)
            # dump offending example batch
            #examples_path = join(workspace.nan_checkpoints, '{}.examples'.format(train_state.train_steps))
            #with open(examples_path, 'w') as f:
            #    pickle.dump(batch, f)
            print('Gradient was NaN/inf on step {}.'.format(train_state.train_steps))

    def eval_and_save(self, editor, step, train_state, config, grad_norm, train_ex, valid_ex):
        # run periodic evaluation and saving
        if step != 0:
            if step % 10 == 0:
                self._update_metadata(train_state)
            if step % config.timing.eval_small == 0:
                self.evaluate(editor, step, train_ex, valid_ex, big_eval=False)
                self.tb_logger.log_value('grad_norm', grad_norm, step)

            if step % config.timing.eval_big == 0:
                self.evaluate(editor, step, train_ex, valid_ex, big_eval=True)
                self.checkpoints.save(train_state)

    def evaluate(self, editor, train_steps, train_ex, valid_ex, big_eval, log=True):
        config = self.config

        def evaluate_on_examples(split_name, examples):
            # use more samples for big evaluation
            num_eval = config.eval.big_num_examples if big_eval else config.eval.num_examples
            big_str = 'big' if big_eval else 'small'

            # compute metrics
            stats, edit_traces, loss_traces = self._compute_metrics(editor, examples, num_eval,
                                                                    self.config.optim.batch_size)

            # prefix the stats
            stats = {(big_str, stat, split_name): val for stat, val in stats.items()}

            if log:
                self._log_stats(stats, train_steps)

                # write traces for the small evaluation
                if not big_eval:
                    self._write_traces(split_name, train_steps, edit_traces, loss_traces)

            return stats

        train_stats = evaluate_on_examples('train', train_ex)
        valid_stats = evaluate_on_examples('valid', valid_ex)

        return train_stats, valid_stats

    @classmethod
    def _compute_metrics(cls, editor, examples, num_evaluate_examples, batch_size):
        """

        Args:
            editor (Editor)
            examples (list[EditExample])
            num_evaluate_examples (int)
            batch_size (int)

        Returns:
            stats (dict[str, float])
            edit_traces (list[EditTrace]): of length num_evaluate_examples
            loss_traces (list[LossTrace]): of length num_evaluate_examples

        """
        sample = sample_if_large(examples, num_evaluate_examples, replace=False)

        # compute loss
        # need to break the sample into batches, in case the sample is too large to fit in GPU memory
        losses, loss_traces, weights, enc_losses = [], [], [], []

        for batch in verboserate(chunks(sample, batch_size), desc='Computing loss on examples'):
            weights.append(len(batch))
            loss_var, loss_trace_batch, enc_loss = editor.loss(batch)

            # convert loss Variable into float
            loss_val = loss_var.data[0]
            assert isinstance(loss_val, float)
            losses.append(loss_val)
            enc_losses.append(enc_loss)

            loss_traces.extend(loss_trace_batch)

        losses, weights = np.array(losses), np.array(weights)
        loss = np.sum(losses * weights) / np.sum(weights)  # weighted average
        enc_loss = np.sum(np.array(enc_losses) * weights) / np.sum(weights)

        punct_table = dict.fromkeys(
            # i for i in xrange(sys.maxunicode) if unicodedata.category(unichr(i)).startswith('P')
            i for i in range(sys.maxunicode) if unicodedata.category(chr(i)).startswith('P') # Alex
        )

        def remove_punct(s):
            new_s = []
            for t in s:
#                 t = unicode(t).translate(punct_table)
                t = str(t).translate(punct_table) # Alex
                if t != '':
                    new_s.append(t)
            return new_s

        metrics = {
            'bleu': (bleu, max),
            'edit_dist': (lambda s, t: edit_dist(s, t)[0] / len(s) if len(s) > 0 else len(t), min),
            'exact_match': (lambda s, t: 1.0 if remove_punct(s) == remove_punct(t) else 0.0, max)
        }

        top_results = defaultdict(list)
        top5_results = defaultdict(list)

        # compute predictions
        beams, edit_traces = editor.edit(sample, batch_size=batch_size, max_seq_length=150, verbose=True)
        for ex, beam in izip(sample, beams):
            top = beam[0]
            top5 = beam[:5]
            target = ex.target_words
            for name, (fxn, best) in metrics.items():
                top_results[name].append(fxn(top, target))
                top5_results[name].append(best(fxn(predict, target) for predict in top5))

        # compute averages
        stats_top = {name: np.mean(vals) for name, vals in top_results.items()}
        stats_top5 = {'{}_top5'.format(name): np.mean(vals) for name, vals in top5_results.items()}

        # combine into a single stats object
        stats = {'loss': loss, 'enc_loss': enc_loss}
        stats.update(stats_top)
        stats.update(stats_top5)

        return stats, edit_traces, loss_traces

    def _write_traces(self, split_name, train_steps, edit_traces, loss_traces):
        trace_dir = join(self.workspace.traces, split_name)
        trace_path = join(trace_dir, '{}.txt'.format(train_steps))
        makedirs(trace_dir)

        with codecs.open(trace_path, 'w', encoding='utf-8') as f:
            for edit_trace, loss_trace in zip(edit_traces, loss_traces):
                f.write(unicode(edit_trace))
                f.write('\n')
                f.write(unicode(loss_trace))
                f.write('\n\n')

