# Copyright 2017 Natural Language Processing Group, Nanjing University, zhaocq.nlp@gmail.com.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""" Base Decoder class and dynamic decode function. """
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from abc import abstractmethod, abstractproperty
from tensorflow.python.util import nest
import tensorflow as tf

from njunmt.utils.global_names import ModeKeys
from njunmt.utils.configurable import Configurable
from njunmt.utils.beam_search import stack_beam_size
from njunmt.utils.beam_search import gather_states
from njunmt.utils.beam_search import BeamSearchStateSpec
from njunmt.utils.expert_utils import DecoderOutputRemover


class Decoder(Configurable):
    """Base class for decoders. """

    def __init__(self, params, mode, name=None, verbose=True):
        """ Initializes the parameters of the decoder.

        Args:
            params: A dictionary of parameters to construct the
              decoder architecture.
            mode: A mode.
            name: The name of this decoder.
            verbose: Print decoder parameters if set True.
        """
        super(Decoder, self).__init__(
            params=params, mode=mode, verbose=verbose,
            name=name or self.__class__.__name__)

    @staticmethod
    def default_params():
        """ Returns a dictionary of default parameters of this decoder. """
        raise NotImplementedError

    @abstractproperty
    def output_dtype(self):
        """ Returns a `collections.namedtuple`,
        the definition of decoder output types. """
        raise NotImplementedError

    @abstractmethod
    def prepare(self, encoder_output, bridge, helper):
        """ Prepares for `step()` function.
        For example,
            1. initialize decoder hidden states (for RNN decoders);
            2. acquire attention information from `encoder_output`;
            3. pre-project the attention values if needed
            4. ...

        Args:
            encoder_output: An instance of `collections.namedtuple`
              from `Encoder.encode()`.
            bridge: An instance of `Bridge` that initializes the
              decoder states.
            helper: An instance of `Feedback` that samples next
              symbols from logits.
        Returns: A tuple `(init_decoder_states, decoding_params)`.
          `decoding_params` is a tuple containing attention values,
          or empty tuple for decoders with no attention mechanism,
          and will be passed to `step()` function.
        """
        raise NotImplementedError

    @abstractmethod
    def step(self, decoder_input, decoder_states, decoding_params):
        """ Decodes one step.

        Args:
            decoder_input: The decoder input for this timestep, an
              instance of `tf.Tensor`.
            decoder_states: The decoder states at previous timestep.
              Must have the same structure with `init_decoder_states`
              returned from `prepare()` function.
            decoding_params: The same as `decoding_params` returned
              from `prepare()` function.

        Returns: A tuple `(cur_decoder_outputs, cur_decoder_states)`
          at this timestep. The `cur_decoder_outputs` must be an
          instance of `collections.namedtuple` whose element types
          are defined by `output_dtype` property. The
          `cur_decoder_states` must have the same structure with
          `decoder_states`.
        """
        raise NotImplementedError

    @abstractmethod
    def merge_top_features(self, decoder_output):
        """ Merges features of decoder top layers, as the input
        of softmax layer.

        Args:
            decoder_output: An instance of `collections.namedtuple`
              whose element types are defined by `output_dtype`
              property.

        Returns: A instance of `tf.Tensor`, as the input of
          softmax layer.
        """
        raise NotImplementedError

    @property
    def output_ignore_fields(self):
        """ Returns a list/tuple of strings. The loop in
        `dynamic_decode` function will not save these fields in
        `output_dtype` during inference, for the sake of reducing
        device memory.
        """
        return None

    def inputs_prepost_processing_fn(self):
        """ This function is for generalization purpose. For `tf.while_loop`
        in `dynamic_decode` function, do some preprocessing to the
        inputs before it is passed to `step()` fn, and do some
        postprocessing to the predictions before it is passed to
        `tf.while_loop`.

        For RNN decoders,  it is not recommended to overwrite this function.

        Returns: A tuple `(preprocessing_fn, postprocessing_fn)`.
        """
        preprocessing_fn = lambda time, inputs: inputs
        postprocessing_fn = lambda prev_inputs, predicted_inputs: predicted_inputs
        return preprocessing_fn, postprocessing_fn

    def decode(self, encoder_output, bridge, helper,
               target_modality):
        """ Decodes one sample.

        Args:
            encoder_output: An instance of `collections.namedtuple`
              from `Encoder.encode()`.
            bridge: An instance of `Bridge` that initializes the
              decoder states.
            helper: An instance of `Feedback` that samples next
              symbols from logits.
            target_modality: An instance of `Modality`, that deals
              with transformations from symbols to tensors or from
              tensors to symbols (the decoder top and bottom layer).

        Returns: A tuple `(decoder_output, decoder_status)`. The
          `decoder_output` is an instance of `collections.namedtuple`
          whose element types are defined by `output_dtype` property.
          For mode=INFER, the `decoder_status` is an instance of
          `collections.namedtuple` whose element types are defined by
          `BeamSearchStateSpec`, indicating the status of beam search.
          For mode=TRAIN/EVAL, the `decoder_status` is a `tf.Tensor`
          indicating logits (computed by `target_modality`), of shape
          [timesteps, batch_size, vocab_size].
        """
        ret_val = dynamic_decode(
            decoder=self,
            encoder_output=encoder_output,
            bridge=bridge,
            helper=helper,
            target_modality=target_modality)
        if self.mode == ModeKeys.INFER:
            outputs, infer_stat = ret_val
            return outputs, infer_stat
        logits = _compute_logits(self, target_modality, ret_val)
        return ret_val, logits


def _compute_logits(decoder, target_modality, decoder_output):
    """ Computes logits.

    Args:
        decoder: An instance of `Decoder.
        target_modality: An instance of `Modality`.
        decoder_output: An instance of `collections.namedtuple`
        whose element types are defined by `decoder.output_dtype`.

    Returns: A `tf.Tensor`.
    """
    with tf.variable_scope(decoder.name):
        decoder_top_features = decoder.merge_top_features(decoder_output)
    with tf.variable_scope(target_modality.name):
        logits = target_modality.top(decoder_top_features)
    return logits


def _embed_words(target_modality, symbols, time):
    """ Embeds words into embeddings.

    Calls prepare() once and step() repeatedly on `Decoder` object.

    Args:
        target_modality: A `Modality` object.
        symbols: A `tf.Tensor` of 1-d, [batch_size, ].
        time: An integer or a scalar int32 tensor,
          indicating the position of this batch of symbols.

    Returns: A `tf.Tensor`, [batch_size, dimension].
    """
    with tf.variable_scope(target_modality.name):
        embeddings = target_modality.targets_bottom(symbols, time=time)
        return embeddings


def dynamic_decode(decoder,
                   encoder_output,
                   bridge,
                   helper,
                   target_modality,
                   parallel_iterations=32,
                   swap_memory=False):
    """ Performs dynamic decoding with `decoder`.

    Call `prepare()` once and `step()` repeatedly on the `Decoder` object.

    Args:
        decoder: An instance of `Decoder`.
        encoder_output: An instance of `collections.namedtuple`
          from `Encoder.encode()`.
        bridge: An instance of `Bridge` that initializes the
          decoder states.
        helper: An instance of `Feedback` that samples next
          symbols from logits.
        target_modality: An instance of `Modality`, that deals
          with transformations from symbols to tensors or from
          tensors to symbols (the decoder top and bottom layer).
        parallel_iterations: Argument passed to `tf.while_loop`.
        swap_memory: Argument passed to `tf.while_loop`.

    Returns: A tuple `(decoder_output, infer_status_tuple)` for
      decoder.mode=INFER.
      `decoder_output` for decoder.mode=TRAIN/INFER.
    """
    var_scope = tf.get_variable_scope()
    # Properly cache variable values inside the while_loop
    if var_scope.caching_device is None:
        var_scope.set_caching_device(lambda op: op.device)

    def _create_ta(d):
        return tf.TensorArray(
            dtype=d, clear_after_read=False,
            size=0, dynamic_size=True)

    decoder_output_remover = DecoderOutputRemover(
        decoder.mode, decoder.output_dtype._fields, decoder.output_ignore_fields)

    # initialize first inputs (start of sentence) with shape [_batch*_beam,]
    initial_finished, initial_input_symbols = helper.init_symbols()
    initial_time = tf.constant(0, dtype=tf.int32)
    initial_inputs = _embed_words(target_modality, initial_input_symbols, initial_time)

    with tf.variable_scope(decoder.name):
        inputs_preprocessing_fn, inputs_postprocessing_fn = decoder.inputs_prepost_processing_fn()
        initial_inputs = inputs_postprocessing_fn(None, initial_inputs)
        initial_decoder_states, decoding_params = decoder.prepare(encoder_output, bridge, helper)  # prepare decoder
        if decoder.mode == ModeKeys.INFER:
            initial_decoder_states = stack_beam_size(initial_decoder_states, helper.beam_size)
            decoding_params = stack_beam_size(decoding_params, helper.beam_size)

    initial_outputs_ta = nest.map_structure(
        _create_ta, decoder_output_remover.apply(decoder.output_dtype))

    def body_traininfer(time, inputs, decoder_states, outputs_ta,
                        finished, *args):
        """Internal while_loop body.

        Args:
          time: scalar int32 Tensor.
          inputs: The inputs Tensor.
          decoder_states: The decoder states.
          outputs_ta: structure of TensorArray.
          finished: A bool tensor (keeping track of what's finished).
          args: The log_probs, lengths, infer_status for mode==INFER.
        Returns:
          `(time + 1, next_inputs, next_decoder_states, outputs_ta,
          next_finished, *args)`.
        """
        with tf.variable_scope(decoder.name):
            inputs = inputs_preprocessing_fn(time, inputs)
            outputs, next_decoder_states = decoder.step(inputs, decoder_states, decoding_params)
        outputs_ta = nest.map_structure(lambda ta, out: ta.write(time, out),
                                        outputs_ta, decoder_output_remover.apply(outputs))
        inner_loop_vars = [time + 1, None, None, outputs_ta, None]
        sample_ids = None
        prev_inputs = inputs
        if decoder.mode == ModeKeys.INFER:
            log_probs, lengths = args[0], args[1]
            infer_status_ta = args[2]
            logits = _compute_logits(decoder, target_modality, outputs)
            # sample next symbols
            sample_ids, beam_ids, next_log_probs, next_lengths \
                = helper.sample_symbols(logits, log_probs, finished, lengths, time=time)

            next_decoder_states = gather_states(next_decoder_states, beam_ids)
            prev_inputs = gather_states(inputs, beam_ids)
            infer_status = BeamSearchStateSpec(
                log_probs=next_log_probs,
                predicted_ids=sample_ids,
                beam_ids=beam_ids,
                lengths=next_lengths)
            infer_status_ta = nest.map_structure(lambda ta, out: ta.write(time, out),
                                                 infer_status_ta, infer_status)
            inner_loop_vars.extend([next_log_probs, next_lengths, infer_status_ta])

        next_finished, next_input_symbols = helper.next_symbols(time=time, sample_ids=sample_ids)
        next_inputs = _embed_words(target_modality, next_input_symbols, time + 1)
        with tf.variable_scope(decoder.name):
            next_inputs = inputs_postprocessing_fn(prev_inputs, next_inputs)

        next_finished = tf.logical_or(next_finished, finished)
        inner_loop_vars[1] = next_inputs
        inner_loop_vars[2] = next_decoder_states
        inner_loop_vars[4] = next_finished
        return inner_loop_vars

    loop_vars = [initial_time, initial_inputs, initial_decoder_states,
                 initial_outputs_ta, initial_finished]

    if decoder.mode == ModeKeys.INFER:  # add inference-specific parameters
        initial_log_probs = tf.zeros_like(initial_input_symbols, dtype=tf.float32)
        initial_lengths = tf.zeros_like(initial_input_symbols, dtype=tf.int32)
        initial_infer_status_ta = nest.map_structure(_create_ta, BeamSearchStateSpec.dtypes())
        loop_vars.extend([initial_log_probs, initial_lengths, initial_infer_status_ta])

    res = tf.while_loop(
        lambda *args: tf.logical_not(tf.reduce_all(args[4])),
        body_traininfer,
        loop_vars=loop_vars,
        parallel_iterations=parallel_iterations,
        swap_memory=swap_memory)

    final_outputs_ta = res[3]
    final_outputs = nest.map_structure(lambda ta: ta.stack(), final_outputs_ta)

    if decoder.mode == ModeKeys.INFER:
        final_infer_status = nest.map_structure(lambda ta: ta.stack(), res[-1])
        return final_outputs, final_infer_status

    return final_outputs
