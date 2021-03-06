import os
import datetime
import logging

import tqdm
import tensorflow as tf
import numpy as np

from agnfinder.tf_sampling.api import get_log_prob_fn


def optimised_start(forward_model, observations, fixed_params, uncertainty, param_dim, n_chains, steps, n_attempts=15, max_cost=10**2.5):
    start_time = datetime.datetime.now()
    best_params, lowest_costs = find_best_params(forward_model, observations, fixed_params, uncertainty, param_dim, n_chains, steps, n_attempts)
    end_time = datetime.datetime.now()
    elapsed = (end_time - start_time).total_seconds()
    logging.info(f'costs: {np.log10(lowest_costs)}')
    logging.info('Done {} optimisations over {} steps in {} seconds.'.format(n_chains, steps, elapsed))
    # require a minimum quality of fit
    below_max_cost = lowest_costs < max_cost
    # set any best_params with really extreme values as failures ?
    # not_extreme_values = np.all(best_params > 0.02, axis=1) & np.all(best_params < 0.98, axis=1)
    # is_successful = below_max_cost & not_extreme_values
    is_successful = below_max_cost
    return best_params, lowest_costs, is_successful


def find_best_params(forward_model, observations, fixed_params, uncertainty, param_dim, batch_dim, steps, n_attempts):
    log_prob_fn = get_log_prob_fn(forward_model, observations, fixed_params, uncertainty)

    # repeatedly run ADAM
    all_params = [[] for n in range(batch_dim)]
    all_costs = [[] for n in range(batch_dim)]
    for _ in tqdm.tqdm(range(n_attempts)):
        initial_params = tf.Variable(tf.random.uniform([batch_dim, param_dim-1], dtype=tf.float32), dtype=tf.float32)  # emulator params
        scale_guess = tf.reduce_sum(-1 * np.log10(observations), axis=1, keepdims=True)
        initial_guess = tf.Variable(tf.concat([initial_params, scale_guess], axis=1))
        initial_guess, latest_costs = find_minima(lambda x: -log_prob_fn(x), initial_guess, steps, param_dim, batch_dim)  # a very important minus sign...

        initial_guess = initial_guess.numpy()
        latest_costs = latest_costs.numpy()
        
        for n in range(batch_dim):
            all_params[n].append(initial_guess[n])
            all_costs[n].append(latest_costs[n])


    best_galaxy_indices = [np.argmin(x) for x in all_costs]
    best_params_by_galaxy = [x[cost] for x, cost in zip(all_params, best_galaxy_indices)]
    best_params = np.array(best_params_by_galaxy)
    lowest_costs = np.array([np.min(x) for x in all_costs])

    return best_params, lowest_costs


# TODO convert to be tf.function() friendly, creating no ops
# TODO measure convergence and adjust steps, learning rate accordingly
def find_minima(func, initial_guess, steps, param_dim, batch_dim):

    # if method == 'bfgs':
        
    #     def value_and_gradients_function(x):
    #         # print(x)
    #         with tf.GradientTape() as tape:
    #             func_value = func(x)
    #             grads = tape.gradient(func_value, [x])[0]
    #             assert grads is not None
    #             return (func_value, grads)
    #     # func_value, gradients = value_and_gradients_function(initial_guess)
    #     # print(gradients)
    #     optim_results = tfp.optimizer.bfgs_minimize(
    #         value_and_gradients_function,
    #         initial_position=initial_guess,
    #         tolerance=1e-8
    #     )
    #     return optim_results.position

    # logging.info('Initial guess neglogprob: {}'.format(['{:4.1f}'.format(x) for x in func(initial_guess)]))
    func_value = tf.Variable(np.zeros(batch_dim), dtype=tf.float32)  # euclid bands hardcoded
    grads = tf.Variable(np.zeros([batch_dim, param_dim]), dtype=tf.float32)
    zeros = tf.constant(np.zeros([batch_dim, 1]), dtype=tf.float32)
    grads_fixed = tf.Variable(np.zeros([batch_dim, param_dim]), dtype=tf.float32)
    method = tf.keras.optimizers.Adam(lr=0.001) # explicit default

    initialise_adam_for_tf(func, initial_guess, method)
    for _ in range(steps):
        update_initial_guess(func, initial_guess, grads, zeros, grads_fixed, method)

    func_value.assign(func(initial_guess))
    return initial_guess, func_value

@tf.function(experimental_compile=True)
def update_initial_guess(func, initial_guess, grads, zeros, grads_fixed, method):
    with tf.GradientTape(watch_accessed_variables=False) as tape:
        tape.watch(initial_guess)
        grads.assign(tape.gradient(func(initial_guess), [initial_guess])[0])
        grads_fixed.assign(tf.concat([grads[:, :-1], zeros], axis=1))

    method.apply_gradients([(grads_fixed, initial_guess)])  # inplace
    scale = tf.expand_dims(initial_guess[:, -1], axis=1)
    params = initial_guess[:, :-1]
    params = tf.clip_by_value(params, 0.01, 0.99)

    initial_guess.assign(tf.concat([params, scale], axis=1))
    # return initial_guess, func_value
    # return func_value

def initialise_adam_for_tf(func, initial_guess, method):
    with tf.GradientTape() as tape:
        tape.watch(initial_guess)
        grads = tape.gradient(func(initial_guess), [initial_guess])[0]
        zeros = tf.zeros((len(grads), 1))
        grads_fixed = tf.concat([grads[:, :-1], zeros], axis=1)
    method.apply_gradients([(grads_fixed, initial_guess)])  # inplace


def many_random_starts(forward_model, observation, param_dim, n_chains, overproposal_factor=None):
    raise NotImplementedError('Deprecated now that each chain is a galaxy')
    # if overproposal_factor is None:
    #     assert n_chains < 100
    #     overproposal_factor = int(10 - (7 * 0.01 * n_chains))  # down from 10. Customised for laptop memory TODO tweak for Glamdring?
    # overproposed_initial_state = tf.random.uniform(shape=(n_chains * overproposal_factor, param_dim))
    # initial_state = keep_top_params(
    #     overproposed_initial_state,
    #     forward_model,
    #     observation,
    #     n_chains * overproposal_factor,  # specified explicitly but might not need to
    #     n_chains)
    # return initial_state

def keep_top_params(all_params, forward_model, true_observation, fixed_params, uncertainty, initial_dim, final_dim):
    log_prob_fn = get_log_prob_fn(forward_model, true_observation, fixed_params, uncertainty)
    initial_log_probs = log_prob_fn(all_params)
    initial_state = tf.gather(all_params, tf.argsort(initial_log_probs, direction='DESCENDING'))[:final_dim]
    return initial_state

def roughly_correct_start(true_params, n_chains):
    not_quite_true_params = true_params + np.random.rand(*true_params.shape) * 0.03
    initial_state = tf.constant(not_quite_true_params, dtype=tf.float32)
    return initial_state
