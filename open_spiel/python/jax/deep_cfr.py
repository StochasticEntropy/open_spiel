# Copyright 2019 DeepMind Technologies Ltd. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Implements Deep CFR Algorithm.

See https://arxiv.org/abs/1811.00164.

The algorithm defines an `advantage` and `strategy` networks that compute
advantages used to do regret matching across information sets and to approximate
the strategy profiles of the game. To train these networks a reservoir buffer
(other data structures may be used) memory is used to accumulate samples to
train the networks.

This implementation uses skip connections as described in the paper if two
consecutive layers of the advantage or policy network have the same number
of units, except for the last connection. Before the last hidden layer
a layer normalization is applied.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import contextlib
import os
import random

import haiku as hk
import jax
import jax.numpy as jnp
import numpy as np
import optax
import tensorflow as tf
import tensorflow_datasets as tfds
tf.config.set_visible_devices([], 'GPU')


from open_spiel.python import policy
import pyspiel

from datetime import datetime

# The size of the shuffle buffer used to reshuffle part of the data each
# epoch within one training iteration
ADVANTAGE_TRAIN_DATASET_SIZE = 100000
STRATEGY_TRAIN_DATASET_SIZE = 1000000


# TODO(author3) Refactor into data structures lib.
class ReservoirBuffer(object):
  """Allows uniform sampling over a stream of data.

  This class supports the storage of arbitrary elements, such as observation
  tensors, integer actions, etc.

  See https://en.wikipedia.org/wiki/Reservoir_sampling for more details.
  """

  def __init__(self, reservoir_buffer_capacity):
    self._reservoir_buffer_capacity = reservoir_buffer_capacity
    self._data = []
    self._add_calls = 0

  def add(self, element):
    """Potentially adds `element` to the reservoir buffer.

    Args:
      element: data to be added to the reservoir buffer.
    """
    if len(self._data) < self._reservoir_buffer_capacity:
      self._data.append(element)
    else:
      idx = np.random.randint(0, self._add_calls + 1)
      if idx < self._reservoir_buffer_capacity:
        self._data[idx] = element
    self._add_calls += 1

  def sample(self, num_samples):
    """Returns `num_samples` uniformly sampled from the buffer.

    Args:
      num_samples: `int`, number of samples to draw.

    Returns:
      An iterable over `num_samples` random elements of the buffer.

    Raises:
      ValueError: If there are less than `num_samples` elements in the buffer
    """
    if len(self._data) < num_samples:
      raise ValueError('{} elements could not be sampled from size {}'.format(
          num_samples, len(self._data)))
    return random.sample(self._data, num_samples)

  def clear(self):
    self._data = []
    self._add_calls = 0

  def __len__(self):
    return len(self._data)

  def __iter__(self):
    return iter(self._data)

  @property
  def data(self):
    return self._data

  def shuffle_data(self):
    random.shuffle(self._data)


class DeepCFRSolver(policy.Policy):
  """Implements a solver for the Deep CFR Algorithm.

  See https://arxiv.org/abs/1811.00164.

  Define all networks and sampling buffers/memories.  Derive losses & learning
  steps. Initialize the game state and algorithmic variables.
  """

  def __init__(self,
               game,
               policy_network_layers=(256, 256),
               advantage_network_layers=(128, 128),
               num_iterations: int = 100,
               num_traversals: int = 100,
               learning_rate: float = 1e-3,
               batch_size_advantage: int = 2048,
               batch_size_strategy: int = 2048,
               memory_capacity: int = int(1e6),
               policy_network_train_steps: int = 5000,
               advantage_network_train_steps: int = 750,
               reinitialize_advantage_networks: bool = True,
               save_advantage_networks: str = None,
               save_strategy_memories: str = None,
               infer_device='cpu',
               train_device='cpu'):
    """Initialize the Deep CFR algorithm.

    Args:
      game: Open Spiel game.
      policy_network_layers: (list[int]) Layer sizes of strategy net MLP.
      advantage_network_layers: (list[int]) Layer sizes of advantage net MLP.
      num_iterations: Number of iterations.
      num_traversals: Number of traversals per iteration.
      learning_rate: Learning rate.
      batch_size_advantage: (int) Batch size to sample from advantage memories.
      batch_size_strategy: (int) Batch size to sample from strategy memories.
      memory_capacity: Number of samples that can be stored in memory.
      policy_network_train_steps: Number of policy network training steps (one
        policy training iteration at the end).
      advantage_network_train_steps: Number of advantage network training steps
        (per iteration).
      reinitialize_advantage_networks: Whether to re-initialize the advantage
        network before training on each iteration.
      save_advantage_networks: If provided, all advantage network itearations
        are saved in the given folder. This can be useful to implement SD-CFR
        https://arxiv.org/abs/1901.07621
      save_strategy_memories: saves the collected strategy memories as a
        tfrecords file in the given location. This is not affected by
        memory_capacity. All memories are saved to disk and not kept in memory
      infer_device: device used for TF-operations in the traversal branch.
        Format is anything accepted by tf.device
      train_device: device used for TF-operations in the NN training steps.
        Format is anything accepted by tf.device
    """
    all_players = list(range(game.num_players()))
    super(DeepCFRSolver, self).__init__(game, all_players)
    self._game = game
    if game.get_type().dynamics == pyspiel.GameType.Dynamics.SIMULTANEOUS:
      # `_traverse_game_tree` does not take into account this option.
      raise ValueError('Simulatenous games are not supported.')
    self._batch_size_advantage = batch_size_advantage
    self._batch_size_strategy = batch_size_strategy
    self._policy_network_train_steps = policy_network_train_steps
    self._advantage_network_train_steps = advantage_network_train_steps
    self._policy_network_layers = policy_network_layers
    self._advantage_network_layers = advantage_network_layers
    self._num_players = game.num_players()
    self._root_node = self._game.new_initial_state()
    self._embedding_size = len(self._root_node.information_state_tensor(0))
    self._num_iterations = num_iterations
    self._num_traversals = num_traversals
    self._reinitialize_advantage_networks = reinitialize_advantage_networks
    self._num_actions = game.num_distinct_actions()
    self._iteration = 1
    self._learning_rate = learning_rate
    self._rngkey = jax.random.PRNGKey(42)

    # Initialize networks
    def base_network(x, layers):
      x = hk.nets.MLP(layers[:-1], activate_final=True)(x)
      x = hk.LayerNorm(axis=-1,
              create_scale=True,
              create_offset=True)(x)
      x = hk.Linear(layers[-1])(x)
      x = jax.nn.relu(x)
      x = hk.Linear(self._num_actions)(x)
      return x
    
    def adv_network(x, mask):
      x = base_network(x, advantage_network_layers)
      x = mask * x
      return x

    def policy_network(x, mask):
      x = base_network(x, policy_network_layers)
      x = jnp.where(mask == 1, x, -10e20)
      x = jax.nn.softmax(x)
      return x

    x, mask = (jnp.ones([1, self._embedding_size]),
                            jnp.ones([1, self._num_actions]))
    self._hk_adv_network = hk.without_apply_rng(hk.transform(adv_network))
    self._params_adv_network = [self._hk_adv_network.init(self._next_rng_key(),
                            x, mask) for _ in range(self._num_players)]
    self._hk_policy_network = hk.without_apply_rng(hk.transform(policy_network))
    self._params_policy_network = self._hk_policy_network.init(self._next_rng_key(),
                            x, mask)

    self._adv_loss = optax.l2_loss
    self._policy_loss = optax.l2_loss
    self._adv_grads = jax.value_and_grad(self._loss_adv)
    self._policy_grads = jax.value_and_grad(self._loss_policy)

    self._opt_adv_init, self._opt_adv_update = optax.adam(learning_rate)
    self._opt_adv_state = [self._opt_adv_init(params)
                            for params in self._params_adv_network]
    self._opt_policy_init, self._opt_policy_update = optax.adam(learning_rate)
    self._opt_policy_state = self._opt_policy_init(self._params_policy_network)

    self._create_memories(memory_capacity)

    self._jitted_maxed_regrets = jax.jit(self._get_jitted_matched_regrets())
    self._jitted_adv_update = jax.jit(self.get_jitted_adv_update())
    self._jitted_policy_update = jax.jit(self.get_jitted_policy_update())

  def get_jitted_adv_update(self):
    def update(params_adv, opt_state, info_states, samp_regrets, iterations, masks,
            total_iterations):
      print('jitting')
      main_loss, grads = self._adv_grads(params_adv,
            info_states, samp_regrets, iterations, masks, total_iterations)
      updates, new_opt_state = self._opt_adv_update(
            grads, opt_state)
      new_params = optax.apply_updates(
              params_adv, updates)
      return new_params, new_opt_state, main_loss
      
    return update

  def get_jitted_policy_update(self):
    def update(params_policy, opt_state, info_states, action_probs, iterations, masks,
            total_iterations):
      print('jitting')
      main_loss, grads = self._policy_grads(params_policy,
            info_states, action_probs, iterations, masks, total_iterations)
      updates, new_opt_state = self._opt_policy_update(
            grads, opt_state)
      new_params = optax.apply_updates(
              params_policy, updates)
      return new_params, new_opt_state, main_loss
      
    return update

  def _get_jitted_matched_regrets(self):
    def get_matched_regrets(info_state, legal_actions_mask, params_adv):
      """TF-Graph to calculate regret matching."""
      #print('jitting')
      advs = self._hk_adv_network.apply(params_adv,
            info_state, legal_actions_mask)
      advantages = jnp.maximum(advs, 0)
      summed_regret = jnp.sum(advantages)
      matched_regrets = jax.lax.cond(summed_regret > 0,
        lambda _: advantages / summed_regret,
        lambda _: jax.nn.one_hot(
            jnp.argmax(jnp.where(legal_actions_mask == 1, advs, -10e20)),
            self._num_actions),
        None)
      # if summed_regret > 0:
      #   matched_regrets = advantages / summed_regret
      # else:
      #   matched_regrets = jax.nn.one_hot(
      #       jnp.argmax(jnp.where(legal_actions_mask == 1, advs, -10e20)),
      #       self._num_actions)
      return advantages, matched_regrets
    return get_matched_regrets

  def _next_rng_key(self):
    self._rngkey, subkey = jax.random.split(self._rngkey)
    return subkey

  def _reinitialize_policy_network(self):
    """Reinitalize policy network and optimizer for training."""
    x, mask = (jnp.ones([1, self._embedding_size]),
                            jnp.ones([1, self._num_actions]))
    self._params_policy_network = self._hk_policy_network.init(self._next_rng_key(),
                            x, mask)
    self._opt_policy_state = self._opt_policy_init(self._params_policy_network)

  def _reinitialize_advantage_network(self, player):
    """Reinitalize player's advantage network and optimizer for training."""
    x, mask = (jnp.ones([1, self._embedding_size]),
                            jnp.ones([1, self._num_actions]))
    self._params_adv_network[player] = self._hk_adv_network.init(
                            self._next_rng_key(), x, mask)
    self._opt_adv_state[player] = self._opt_adv_init(
                            self._params_adv_network[player])

  @property
  def advantage_buffers(self):
    return self._advantage_memories

  @property
  def strategy_buffer(self):
    return self._strategy_memories

  def clear_advantage_buffers(self):
    for p in range(self._num_players):
      self._advantage_memories[p].clear()

  def _create_memories(self, memory_capacity):
    """Create memory buffers"""
    self._strategy_memories = ReservoirBuffer(memory_capacity)
    self._advantage_memories = [
        ReservoirBuffer(memory_capacity) for _ in range(self._num_players)
    ]
    self._strategy_feature_description = {
        'info_state': tf.io.FixedLenFeature([self._embedding_size], tf.float32),
        'action_probs': tf.io.FixedLenFeature([self._num_actions], tf.float32),
        'iteration': tf.io.FixedLenFeature([1], tf.float32),
        'legal_actions': tf.io.FixedLenFeature([self._num_actions], tf.float32)
    }
    self._advantage_feature_description = {
        'info_state': tf.io.FixedLenFeature([self._embedding_size], tf.float32),
        'iteration': tf.io.FixedLenFeature([1], tf.float32),
        'samp_regret': tf.io.FixedLenFeature([self._num_actions], tf.float32),
        'legal_actions': tf.io.FixedLenFeature([self._num_actions], tf.float32)
    }

  def solve(self):
    """Solution logic for Deep CFR."""
    advantage_losses = collections.defaultdict(list)
    for _ in range(self._num_iterations):
      for p in range(self._num_players):
        for _ in range(self._num_traversals):
          self._traverse_game_tree(self._root_node, p)
        if self._reinitialize_advantage_networks:
          # Re-initialize advantage network for p and train from scratch.
          self._reinitialize_advantage_network(p)
        advantage_losses[p].append(self._learn_advantage_network(p))
      self._iteration += 1
    # Train policy network.
    policy_loss = self._learn_strategy_network()
    return None, advantage_losses, policy_loss


  def _serialize_advantage_memory(self, info_state, iteration, samp_regret,
                                  legal_actions_mask):
    """Create serialized example to store an advantage entry."""
    example = tf.train.Example(
        features=tf.train.Features(
            feature={
                'info_state':
                    tf.train.Feature(
                        float_list=tf.train.FloatList(value=info_state)),
                'iteration':
                    tf.train.Feature(
                        float_list=tf.train.FloatList(value=[iteration])),
                'samp_regret':
                    tf.train.Feature(
                        float_list=tf.train.FloatList(value=samp_regret)),
                'legal_actions':
                    tf.train.Feature(
                        float_list=tf.train.FloatList(value=legal_actions_mask))
            }))
    return example.SerializeToString()

  def _deserialize_advantage_memory(self, serialized):
    """Deserializes a batch of advantage examples for the train step."""
    tups = tf.io.parse_example(serialized, self._advantage_feature_description)
    return (tups['info_state'], tups['samp_regret'], tups['iteration'],
            tups['legal_actions'])

  def _serialize_strategy_memory(self, info_state, iteration,
                                 strategy_action_probs, legal_actions_mask):
    """Create serialized example to store a strategy entry."""
    example = tf.train.Example(
        features=tf.train.Features(
            feature={
                'info_state':
                    tf.train.Feature(
                        float_list=tf.train.FloatList(value=info_state)),
                'action_probs':
                    tf.train.Feature(
                        float_list=tf.train.FloatList(
                            value=strategy_action_probs)),
                'iteration':
                    tf.train.Feature(
                        float_list=tf.train.FloatList(value=[iteration])),
                'legal_actions':
                    tf.train.Feature(
                        float_list=tf.train.FloatList(value=legal_actions_mask))
            }))
    return example.SerializeToString()

  def _deserialize_strategy_memory(self, serialized):
    """Deserializes a batch of strategy examples for the train step."""
    tups = tf.io.parse_example(serialized, self._strategy_feature_description)
    return (tups['info_state'], tups['action_probs'], tups['iteration'],
            tups['legal_actions'])

  def _add_to_strategy_memory(self, info_state, iteration,
                              strategy_action_probs, legal_actions_mask):
    # pylint: disable=g-doc-args
    """Adds the given strategy data to the memory.

    Uses either a tfrecordsfile on disk if provided, or a reservoir buffer.
    """
    serialized_example = self._serialize_strategy_memory(
        info_state, iteration, strategy_action_probs, legal_actions_mask)
    self._strategy_memories.add(serialized_example)

  def _traverse_game_tree(self, state, player):
    """Performs a traversal of the game tree using external sampling.

    Over a traversal the advantage and strategy memories are populated with
    computed advantage values and matched regrets respectively.

    Args:
      state: Current OpenSpiel game state.
      player: (int) Player index for this traversal.

    Returns:
      Recursively returns expected payoffs for each action.
    """
    if state.is_terminal():
      # Terminal state get returns.
      return state.returns()[player]
    elif state.is_chance_node():
      # If this is a chance node, sample an action
      action = np.random.choice([i[0] for i in state.chance_outcomes()])
      return self._traverse_game_tree(state.child(action), player)
    elif state.current_player() == player:
      # Update the policy over the info set & actions via regret matching.
      _, strategy = self._sample_action_from_advantage(state, player)
      strategy = np.array(strategy)
      exp_payoff = 0 * strategy
      for action in state.legal_actions():
        exp_payoff[action] = self._traverse_game_tree(
            state.child(action), player)
      ev = np.sum(exp_payoff * strategy)
      samp_regret = (exp_payoff - ev) * state.legal_actions_mask(player)
      self._advantage_memories[player].add(
          self._serialize_advantage_memory(state.information_state_tensor(),
                                           self._iteration, samp_regret,
                                           state.legal_actions_mask(player)))
      # self._advantage_memories[player].add((state.information_state_tensor(),
      #                                      self._iteration, samp_regret,
      #                                      state.legal_actions_mask(player)))
      return ev
    else:
      other_player = state.current_player()
      _, strategy = self._sample_action_from_advantage(state, other_player)
      strategy = np.array(strategy)
      # Recompute distribution for numerical errors.
      probs = strategy
      probs /= probs.sum()
      sampled_action = np.random.choice(range(self._num_actions), p=probs)
      self._add_to_strategy_memory(
          state.information_state_tensor(other_player), self._iteration,
          strategy, state.legal_actions_mask(other_player))
      return self._traverse_game_tree(state.child(sampled_action), player)

  # #@tf.function
  # #@jax.partial(jax.jit, static_argnums=3)
  # def _get_matched_regrets(self, info_state, legal_actions_mask, player):
  #   """TF-Graph to calculate regret matching."""
  #   print('jitting')
  #   advs = self._hk_adv_network.apply(self._params_adv_network[player],
  #         info_state, legal_actions_mask)
  #   advantages = jnp.maximum(advs, 0)
  #   summed_regret = jnp.sum(advantages)
  #   # if summed_regret > 0:
  #   matched_regrets = advantages / summed_regret
  #   # else:
  #   #   matched_regrets = jax.nn.one_hot(
  #   #       jnp.argmax(jnp.where(legal_actions_mask == 1, advs, -10e20)),
  #   #       self._num_actions)
  #   return advantages, matched_regrets

  def _sample_action_from_advantage(self, state, player):
    """Returns an info state policy by applying regret-matching.

    Args:
      state: Current OpenSpiel game state.
      player: (int) Player index over which to compute regrets.

    Returns:
      1. (np-array) Advantage values for info state actions indexed by action.
      2. (np-array) Matched regrets, prob for actions indexed by action.
    """
    # @jax.jit
    # def get_matched_regrets(info_state, legal_actions_mask, params_adv):
    #   """TF-Graph to calculate regret matching."""
    #   print('jitting')
    #   advs = self._hk_adv_network.apply(params_adv,
    #         info_state, legal_actions_mask)
    #   advantages = jnp.maximum(advs, 0)
    #   summed_regret = jnp.sum(advantages)
    #   matched_regrets = jax.lax.cond(summed_regret > 0,
    #     lambda _: advantages / summed_regret,
    #     lambda _: jax.nn.one_hot(
    #         jnp.argmax(jnp.where(legal_actions_mask == 1, advs, -10e20)),
    #         self._num_actions),
    #     None)
    #   # if summed_regret > 0:
    #   #   matched_regrets = advantages / summed_regret
    #   # else:
    #   #   matched_regrets = jax.nn.one_hot(
    #   #       jnp.argmax(jnp.where(legal_actions_mask == 1, advs, -10e20)),
    #   #       self._num_actions)
    #   return advantages, matched_regrets

    info_state = jnp.array(
        state.information_state_tensor(player), dtype=jnp.float32)
    legal_actions_mask = jnp.array(
        state.legal_actions_mask(player), dtype=jnp.float32)
    advantages, matched_regrets = self._jitted_maxed_regrets(
        info_state, legal_actions_mask, self._params_adv_network[player])
    return advantages, matched_regrets

  def action_probabilities(self, state):
    """Returns action probabilities dict for a single batch."""
    cur_player = state.current_player()
    legal_actions = state.legal_actions(cur_player)
    # legal_actions_mask = tf.constant(
    #     state.legal_actions_mask(cur_player), dtype=tf.float32)
    # info_state_vector = tf.constant(
    #     state.information_state_tensor(), dtype=tf.float32)
    info_state_vector = jnp.array(
        state.information_state_tensor(), dtype=jnp.float32)
    legal_actions_mask = jnp.array(
        state.legal_actions_mask(cur_player), dtype=jnp.float32)
    # if len(info_state_vector.shape) == 1:
    #   info_state_vector = tf.expand_dims(info_state_vector, axis=0)
    probs = self._hk_policy_network.apply(self._params_policy_network,
          info_state_vector, legal_actions_mask)
    # probs = self._policy_network((info_state_vector, legal_actions_mask),
    #                              training=False)
    # probs = probs.numpy()
    return {action: probs[action] for action in legal_actions}

  def _sample_advantage_dataset(self, player, nr_steps=1):
    """Returns the collected regrets for the given player as a dataset.
    yields list with stacked tensors
          [state.information_state_tensor(),
          self._iteration,
          samp_regret,
          state.legal_actions_mask(player)]
    """
    # bs = min(self._batch_size_advantage, len(self._advantage_memories[player]))
    # for _ in range(nr_steps):
    #   data = self._advantage_memories[player].sample(bs)
    #   restacked = [jnp.stack([jnp.array(x[i]) for x in data]) for i in range(len(data[0]))]
    #   yield restacked

    
    self._advantage_memories[player].shuffle_data()
    self._advantage_memories[player]
    data = tf.data.Dataset.from_tensor_slices(
        self._advantage_memories[player].data)
    data = data.shuffle(ADVANTAGE_TRAIN_DATASET_SIZE)
    data = data.repeat()
    data = data.batch(self._batch_size_advantage)
    data = data.map(self._deserialize_advantage_memory)
    data = data.prefetch(tf.data.experimental.AUTOTUNE)
    data = data.take(nr_steps)
    return iter(tfds.as_numpy(data))

  def _sample_strategy_dataset(self, nr_steps=1):
    """Returns the collected strategy memories as a dataset.
    """
    # s = min(self._batch_size_strategy, len(self._strategy_memories))
    # for _ in range(nr_steps):
    #   data = self._strategy_memories.sample(bs)
    #   restacked = [jnp.stack([jnp.array(x[i]) for x in data]) for i in range(len(data[0]))]
    #   yield restacked

    # if self._memories_tfrecordpath:
    #   data = tf.data.TFRecordDataset(self._memories_tfrecordpath)
    # else:
    self._strategy_memories.shuffle_data()
    data = tf.data.Dataset.from_tensor_slices(self._strategy_memories.data)
    data = data.shuffle(STRATEGY_TRAIN_DATASET_SIZE)
    data = data.repeat()
    data = data.batch(self._batch_size_strategy)
    data = data.map(self._deserialize_strategy_memory)
    data = data.prefetch(tf.data.experimental.AUTOTUNE)
    data = data.take(nr_steps)
    return iter(tfds.as_numpy(data))

  # def _get_advantage_train_graph(self, player):
  #   """Return TF-Graph to perform advantage network train step."""
  #   @tf.function
  #   def train_step(info_states, advantages, iterations, masks, iteration):
  #     model = self._adv_networks_train[player]
  #     with tf.GradientTape() as tape:
  #       preds = model((info_states, masks), training=True)
  #       main_loss = self._loss_advantages[player](
  #           advantages, preds, sample_weight=iterations * 2 / iteration)
  #       loss = tf.add_n([main_loss], model.losses)
  #     gradients = tape.gradient(loss, model.trainable_variables)
  #     self._optimizer_advantages[player].apply_gradients(
  #         zip(gradients, model.trainable_variables))
  #     return main_loss

  #   return train_step

  def _loss_adv(self, params_adv, info_states, samp_regrets, iterations, masks,
            total_iterations):
    preds = self._hk_adv_network.apply(params_adv, info_states, masks)
    loss_values = jnp.mean(self._adv_loss(preds, samp_regrets), axis=-1)
    loss_values = loss_values * iterations * 2 / total_iterations
    return jnp.mean(loss_values)

  

  def _learn_advantage_network(self, player):
    """Compute the loss on sampled transitions and perform a Q-network update.

    If there are not enough elements in the buffer, no loss is computed and
    `None` is returned instead.

    Args:
      player: (int) player index.

    Returns:
      The average loss over the advantage network of the last batch.
    """
    # @jax.jit
    # def update(params_adv, opt_state, info_states, samp_regrets, iterations, masks,
    #         total_iterations):
    #   print('jitting')
    #   main_loss, grads = self._adv_grads(params_adv,
    #         info_states, samp_regrets, iterations, masks, total_iterations)
    #   updates, new_opt_state = self._opt_adv_update(
    #         grads, opt_state)
    #   new_params = optax.apply_updates(
    #           params_adv, updates)
    #   return new_params, new_opt_state, main_loss

    print(datetime.now())
    print(f'iteration {self._iteration}: start training')
    i = 0
    for data in self._sample_advantage_dataset(player,
          self._advantage_network_train_steps):
      # if i%1 == 0:
      #   print(f'a{i}: {datetime.now()}')
      self._params_adv_network[player], self._opt_adv_state[player], main_loss =\
        self._jitted_adv_update(self._params_adv_network[player], self._opt_adv_state[player],
        *data, jnp.array(self._iteration))
      i+=1
      # if i%1 == 0:
      #   print(f'b{i}: {datetime.now()}')
      # if i==self._advantage_network_train_steps:
      #   break
      # main_loss, grads = self._adv_grads(self._params_adv_network[player],
      #       *data, self._iteration)
      # updates, self._opt_adv_state[player] = self._opt_adv_update(
      #       grads, self._opt_adv_state[player])
      # self._params_adv_network[player] = optax.apply_updates(
      #         self._params_adv_network[player], updates)
    
    print(datetime.now())
    print(f'iteration {self._iteration}: finished training')
    # with tf.device(self._train_device):
    #   tfit = tf.constant(self._iteration, dtype=tf.float32)
    #   data = self._get_advantage_dataset(player)
    #   for d in data.take(self._advantage_network_train_steps):
    #     main_loss = self._advantage_train_step[player](*d, tfit)

    # self._adv_networks[player].set_weights(
    #     self._adv_networks_train[player].get_weights())
    return main_loss

  # def _get_strategy_dataset(self):
  #   """Returns the collected strategy memories as a dataset."""
  #   if self._memories_tfrecordpath:
  #     data = tf.data.TFRecordDataset(self._memories_tfrecordpath)
  #   else:
  #     self._strategy_memories.shuffle_data()
  #     data = tf.data.Dataset.from_tensor_slices(self._strategy_memories.data)
  #   data = data.shuffle(STRATEGY_TRAIN_DATASET_SIZE)
  #   data = data.repeat()
  #   data = data.batch(self._batch_size_strategy)
  #   data = data.map(self._deserialize_strategy_memory)
  #   data = data.prefetch(tf.data.experimental.AUTOTUNE)
  #   return data

  def _loss_policy(self, params_policy, info_states, action_probs, iterations,
              masks, total_iterations):
    preds = self._hk_policy_network.apply(params_policy, info_states, masks)
    loss_values = jnp.mean(self._policy_loss(preds, action_probs), axis=-1)
    loss_values = loss_values * iterations * 2 / total_iterations
    return jnp.mean(loss_values)

  def _learn_strategy_network(self):
    """Compute the loss over the strategy network.

    Returns:
      The average loss obtained on the last training batch of transitions
      or `None`.
    """

    # @jax.jit
    # def update(params_policy, opt_state, info_states, action_probs, iterations, masks,
    #         total_iterations):
    #   print('jitting')
    #   main_loss, grads = self._policy_grads(params_policy,
    #         info_states, action_probs, iterations, masks, total_iterations)
    #   updates, new_opt_state = self._opt_policy_update(
    #         grads, opt_state)
    #   new_params = optax.apply_updates(
    #           params_policy, updates)
    #   return new_params, new_opt_state, main_loss

    print(datetime.now())
    print(f'policy: start training')
    i = 0
    for data in self._sample_strategy_dataset(
          self._policy_network_train_steps):
      # if i%1 == 0:
      #   print(f'a{i}: {datetime.now()}')
      self._params_policy_network, self._opt_policy_state, main_loss =\
        self._jitted_policy_update(self._params_policy_network, self._opt_policy_state,
        *data, self._iteration)
      i+=1
      # if i%1 == 0:
      #   print(f'b{i}: {datetime.now()}')
      # if i==self._policy_network_train_steps:
      #   break
      # main_loss, grads = self._adv_grads(self._params_adv_network[player],
      #       *data, self._iteration)
      # updates, self._opt_adv_state[player] = self._opt_adv_update(
      #       grads, self._opt_adv_state[player])
      # self._params_adv_network[player] = optax.apply_updates(
      #         self._params_adv_network[player], updates)
    
    print(datetime.now())
    print(f'policy: finished training')

    # for data in self._sample_strategy_dataset(
    #       self._policy_network_train_steps):
    #   main_loss, grads = self._policy_grads(self._params_policy_network,
    #         *data, self._iteration)
    #   updates, self._opt_policy_state = self._opt_policy_update(
    #         grads, self._opt_policy_state)
    #   self._params_policy_network = optax.apply_updates(
    #           self._params_policy_network, updates)

    # @tf.function
    # def train_step(info_states, action_probs, iterations, masks):
    #   model = self._policy_network
    #   with tf.GradientTape() as tape:
    #     preds = model((info_states, masks), training=True)
    #     main_loss = self._loss_policy(
    #         action_probs, preds, sample_weight=iterations * 2 / self._iteration)
    #     loss = tf.add_n([main_loss], model.losses)
    #   gradients = tape.gradient(loss, model.trainable_variables)
    #   self._optimizer_policy.apply_gradients(
    #       zip(gradients, model.trainable_variables))
    #   return main_loss

    # with tf.device(self._train_device):
    #   data = self._get_strategy_dataset()
    #   for d in data.take(self._policy_network_train_steps):
    #     main_loss = train_step(*d)

    return main_loss
