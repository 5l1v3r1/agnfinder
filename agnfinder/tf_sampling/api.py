import logging

import numpy as np
import tensorflow as tf

from agnfinder.tf_sampling import deep_emulator

class SamplingProblem():

    def __init__(self, observation_ids, true_observation, true_params, forward_model, fixed_params, uncertainty):
        assert len(observation_ids) == len(true_observation) == len(true_params) == len(fixed_params) == len(uncertainty)
        assert true_observation.ndim == 2
        assert true_params.ndim == 2
        self.observation_ids = observation_ids
        self.true_observation = true_observation
        self.true_params = true_params
        self.forward_model = forward_model  # should be a callable. Often, an emulator and (perhaps) a rescaling
        self.fixed_params = fixed_params
        self.uncertainty = uncertainty

    @property
    def param_dim(self):
        return self.true_params.shape[1]  # 0th is batch, 1st is params

    def filter_by_mask(self, mask):  # inplace
        
        # mask may be a tf tensor
        if isinstance(mask, np.ndarray):
            np_mask = mask
        else:
            np_mask = mask.numpy()
        ids_were_adapted = list(zip(self.observation_ids, np_mask))  # don't use dict, galaxy_ids may repeat (many)
        remaining_ids = [k for k, v in ids_were_adapted if v]
        discarded_ids = [k for k, v in ids_were_adapted if not v]
        if len(discarded_ids) != 0:
            logging.warning('Discarding observations: {} '.format(discarded_ids))
        self.observation_ids = remaining_ids
        # now, index of galaxy_ids will match indexes within problem again

        self.true_observation = tf.boolean_mask(
            tensor=self.true_observation,
            mask=mask,
            axis=0
        )
        self.fixed_params = tf.boolean_mask(
            tensor=self.fixed_params,
            mask=mask,
            axis=0
        )
        self.uncertainty = tf.boolean_mask(
            tensor=self.uncertainty,
            mask=mask,
            axis=0
        )
        self.true_params = tf.boolean_mask(
            tensor=self.true_params,
            mask=mask,
            axis=0
        )


class Sampler():

    def __init__(self, problem: SamplingProblem, *args, **kw_args):
        self.problem = problem

    def sample(self):
        raise NotImplementedError

    def __call__(self):
        return self.sample()


# @tf.function  # inputs become tensors when you wrap
def get_log_prob_fn(forward_model, true_photometry, fixed_params, uncertainty):  
    # uncertainty can be scalar, or (batch_dim, photometry_dim)
    assert tf.rank(uncertainty).numpy() == 2
    assert tf.rank(true_photometry).numpy() == 2  # true photometry must be (batch_dim, photometry_dim) even if batch_dim=1
    # first dimension of true params must match first dimension of x, or will fail
    @tf.function(experimental_compile=True)
    def log_prob_fn(x):  # 0th axis is batch/chain dim, 1st is param dim
        # expected photometry has been normalised by deep_emulator.normalise_photometry, remember - it's neg log10 mags
        scale = tf.expand_dims(x[:, -1], 1)  # scale is last column
        params = x[:, :-1]  # emulator_params are all other columns
        params_with_fixed_params = tf.concat([fixed_params, params], axis=1)  # no effect if fixed params is dim0
        normalised_photometry = forward_model(params_with_fixed_params)
        expected_photometry = deep_emulator.denormalise_photometry(normalised_photometry, scale)  # remove the rescaling and then remove the log10 normalisation
        deviation = tf.abs(expected_photometry - true_photometry)   # make sure you denormalise true observation in the first place, if loading from data(). Should be in maggies.
        variance = uncertainty ** 2

        # original version
        # neg_log_prob_by_band = deviation ** 2 / (2*variance)
        # prospector version
        # https://github.com/bd-j/prospector/blob/master/prospect/likelihood/likelihood.py#L142
        neg_log_prob_by_band = 0.5*( (deviation**2/variance) - tf.math.log(2*np.pi*variance) )
        log_prob = -tf.reduce_sum(input_tensor=neg_log_prob_by_band, axis=1)  # log space: product -> sum
        
        x_out_of_bounds = is_out_of_bounds(params)  # being careful to exclude scale
        penalty = tf.cast(x_out_of_bounds, tf.float32) * tf.constant(1e10, dtype=tf.float32)
        log_prob_with_penalty = log_prob - penalty  # no effect if x in bounds, else divide (subtract) a big penalty

        # if np.random.rand() > 0.99:
            # print(params_with_fixed_params.numpy()[0])
            # print(scale.numpy()[0])
            # print(true_photometry.numpy()[0])
            # print(expected_photometry.numpy()[0])
            # print(log_prob_with_penalty.numpy()[0])

        return log_prob_with_penalty
    return log_prob_fn


# straight-up copy of the above, but allowing variable batch size for emcee
# needs updating with scale, once happy
def get_log_prob_fn_variable_batch(forward_model, true_photometry, fixed_params, uncertainty):  
    assert tf.rank(uncertainty).numpy() == 1
    assert tf.rank(true_photometry).numpy() == 1
    @tf.function(experimental_compile=True)
    def log_prob_fn(x):
        batch_dim = tf.shape(x)[0]
        fixed_params_batch = tf.tile(tf.expand_dims(fixed_params, axis=0), [batch_dim, 1])
        true_photometry_batch = tf.tile(tf.expand_dims(true_photometry, axis=0), [batch_dim, 1])
        uncertainty_batch = tf.tile(tf.expand_dims(uncertainty, axis=0), [batch_dim, 1])

        x_with_fixed_params = tf.concat([fixed_params_batch, x], axis=1)  # will hopefully have no effect if fixed params is dim0?
        expected_photometry = deep_emulator.denormalise_photometry(forward_model(x_with_fixed_params))  # model expects a batch dimension, which here is the chains
        deviation = tf.abs(expected_photometry - true_photometry_batch)   # make sure you denormalise true observation in the first place, if loading from data(). Should be in maggies.
        variance = uncertainty_batch ** 2
        neg_log_prob_by_band = 0.5*( (deviation**2/variance) - tf.math.log(2*np.pi*variance) )
        log_prob = -tf.reduce_sum(input_tensor=neg_log_prob_by_band, axis=1)  # log space: product -> sum
        x_out_of_bounds = is_out_of_bounds(x)
        penalty = tf.cast(x_out_of_bounds, tf.float32) * tf.constant(1e10, dtype=tf.float32)
        log_prob_with_penalty = log_prob - penalty  # no effect if x in bounds, else divide (subtract) a big penalty
        return log_prob_with_penalty
    return log_prob_fn


def is_out_of_bounds(x):  # x expected to have (batch, param) shape
    # must not include scale param
    return tf.reduce_any( input_tensor=tf.cast(x > 1., tf.bool) | tf.cast(x < 0., tf.bool),  axis=1)
