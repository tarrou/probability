# Copyright 2019 The TensorFlow Probability Authors.
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
# ============================================================================
"""Utilities for Runge Kutta solvers."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import numpy as np
import tensorflow.compat.v2 as tf
from tensorflow_probability.python.internal import dtype_util


ButcherTableau = collections.namedtuple('ButcherTableau',
                                        ['a', 'b', 'c_sol', 'c_mid', 'c_error'])
# A mnemonic device that organizes coefficients of Runge-Kutta schemes.


def _possibly_nonzero(value):
  """Returns Python boolean indicating whether `value` can be non-zero.

  Args:
    value: `Tensor` or numpy array that is tested on being certainly zero.
      Tensors are considered non-zero.

  Returns:
    possibly_nonzero: `False` if `value` is deterministically zero, `True`
      otherwise.
  """
  static_value = tf.get_static_value(value)
  if static_value is None:
    return True
  else:
    return np.all(static_value != 0)


def _weighted_sum(weights, list_of_states):
  """Computes a weighted sum of `list_of_states`.

  Args:
    weights: List of scalar tensors.
    list_of_states: List of states. Every element is assumed to be of the same
      structure of Tensors. Must be of the same length as `weights`.

  Returns:
    weighted_sum: A weighted sum of states in `list_of_states`. Has the same
      structure as elements of `list_of_states`.

  Raises:
    ValueError: If `list_of_states` is empty or length doesn't match `weights`.
  """
  with tf.name_scope('weighted_sum'):
    if not weights:
      raise ValueError('`list_of_states` and `weights` must be non-empty')
    if len(weights) != len(list_of_states):
      raise ValueError('`weights` and `list_of_states` must have same length')
    for state in list_of_states:
      tf.nest.assert_same_structure(state, list_of_states[-1])
    weights_and_states = zip(weights, list_of_states)
    weighted_states = [
        [w * s_component for s_component in tf.nest.flatten(s)]
        for w, s in weights_and_states if _possibly_nonzero(w)
    ]
    list_of_components = zip(*weighted_states)  # Put same components together.
    flat_final_state = [tf.add_n(component) for component in list_of_components]
    return tf.nest.pack_sequence_as(list_of_states[0], flat_final_state)


def _fourth_order_interpolation_coefficients(y0, y1, y_mid, f0, f1, dt):
  """Fits coefficients for 4th order polynomial interpolation.

  Args:
    y0: state value at the start of the interval.
    y1: state value at the end of the interval.
    y_mid: state value at the mid-point of the interval.
    f0: state derivative value at the start of the interval.
    f1: state derivative value at the end of the interval.
    dt: width of the interval.

  Returns:
    coefficients: List of coefficients `[a, b, c, d, e]` for interpolating with
      the polynomial `p = a * x ** 4 + b * x ** 3 + c * x ** 2 + d * x + e` for
      values of `x` between 0 (start of interval) and 1 (end of interval).
  """
  # Formulas for interpolation coefficients were computed as follows:
  # ```None
  # a, b, c, d, e = sympy.symbols('a b c d e')
  # x, dt, y0, y1, y_mid, f0, f1 = sympy.symbols('x dt y0 y1 y_mid f0 f1')
  # p = a * x ** 4 + b * x ** 3 + c * x ** 2 + d * x + e
  # sympy.solve([p.subs(x, 0) - y0,
  #              p.subs(x, 1 / 2) - y_mid,
  #              p.subs(x, 1) - y1,
  #              (p.diff(x) / dt).subs(x, 0) - f0,
  #              (p.diff(x) / dt).subs(x, 1) - f1],
  #             [a, b, c, d, e])
  # {a: -2.0*dt*f0 + 2.0*dt*f1 - 8.0*y0 - 8.0*y1 + 16.0*y_mid,
  #  b: 5.0*dt*f0 - 3.0*dt*f1 + 18.0*y0 + 14.0*y1 - 32.0*y_mid,
  #  c: -4.0*dt*f0 + dt*f1 - 11.0*y0 - 5.0*y1 + 16.0*y_mid,
  #  d: dt*f0,
  #  e: y0}
  #  ```
  with tf.name_scope('interpolation_coefficients'):
    a = _weighted_sum([-2 * dt, 2 * dt, -8, -8, 16], [f0, f1, y0, y1, y_mid])
    b = _weighted_sum([5 * dt, -3 * dt, 18, 14, -32], [f0, f1, y0, y1, y_mid])
    c = _weighted_sum([-4 * dt, dt, -11, -5, 16], [f0, f1, y0, y1, y_mid])
    d = dt * f0
    e = y0
  return [a, b, c, d, e]


def rk_fourth_order_interpolation_coefficients(y0, y1, k, dt, tableau):
  """Fit an interpolating polynomial to the results of a Runge-Kutta step.

  Performs 4th order interpolation based on state and state derivative values
  determined in the Runge-Kutta state.

  Args:
    y0: State value at the start of the interval.
    y1: State value at the end of the interval.
    k: List of state values at RK k-points.
    dt: Width of the interval.
    tableau: `ButcherTableau` describing a Runge-Kutta scheme.

  Returns:
    coefficients: List of coefficients that interpolate the solution.
  """
  with tf.name_scope('interp_fit_rk'):
    dt = tf.cast(dt, y0.dtype)
    y_mid = y0 + dt * _weighted_sum(tableau.c_mid, k)
    f0 = k[0]
    f1 = k[-1]
    return _fourth_order_interpolation_coefficients(y0, y1, y_mid, f0, f1, dt)


def evaluate_interpolation(coefficients, t0, t1, t):
  """Evaluates the value of polynomial interpolation at the given time point.

  Args:
    coefficients: List of `Tensor`s that hold polynomial coefficients. Must have
      length greater or equal to 2.
    t0: Scalar floating `Tensor` giving the start of the interval.
    t1: Scalar floating `Tensor` giving the end of the interval.
    t: Scalar floating `Tensor` giving the desired interpolation point.

  Returns:
    interpolated_value: Polynomial interpolation at time `t`.

  Raises:
    ValueError: If `coefficients` has less than 2 elements.
  """
  if len(coefficients) < 2:
    raise ValueError('`coefficients` must have at least 2 elements.')
  with tf.name_scope('interp_evaluate'):
    dtype = dtype_util.common_dtype(coefficients)
    t0 = tf.convert_to_tensor(t0)
    t1 = tf.convert_to_tensor(t1)
    t = tf.convert_to_tensor(t)
    assert_op = tf.Assert(
        (t0 <= t) & (t <= t1),
        ['invalid interpolation, fails `t0 <= t <= t1`:', t0, t, t1])
    with tf.control_dependencies([assert_op]):
      x = tf.cast((t - t0) / (t1 - t0), dtype)
    xs = [tf.constant(1, dtype), x]
    for _ in range(2, len(coefficients)):
      xs.append(xs[-1] * x)
    return _weighted_sum(coefficients, list(reversed(xs)))


def runge_kutta_step(ode_fn,
                     y0,
                     f0,
                     t0,
                     dt,
                     tableau,
                     name='runge_kutta_step'):
  """Take an arbitrary Runge-Kutta step and estimate error.

  Args:
    ode_fn: Callable(t, y) -> dy_dt that evaluate the time derivative of `y`.
    y0: `Tensor` initial value for the state.
    f0: `Tensor` initial value for the derivative of `y0` = `ode_fn(t0, y0)`.
    t0: `Tensor` value for the initial time.
    dt: `Tensor` value for the desired time step.
    tableau: `ButcherTableau` describing how to take the Runge-Kutta step.
    name: optional name for the operation.

  Returns:
    rk_state_tuple: Tuple `(y1, f1, y1_error, k)` giving the estimated function
      value after the Runge-Kutta step at `t1 = t0 + dt`, the derivative of the
      state at `t1`, estimated error at `t1`, and a list of Runge-Kutta
      coefficients `k` used for calculating these terms.
  """
  with tf.name_scope(name):
    y0 = tf.convert_to_tensor(y0, name='y0')
    f0 = tf.convert_to_tensor(f0, name='f0')
    t0 = tf.convert_to_tensor(t0, name='t0')
    dt = tf.convert_to_tensor(dt, name='dt')
    dt_cast = tf.cast(dt, y0.dtype)

    k = [f0]
    for alpha_i, beta_i in zip(tableau.a, tableau.b):
      ti = t0 + alpha_i * dt
      yi = y0 + dt_cast * _weighted_sum(beta_i, k)
      k.append(ode_fn(ti, yi))

    if not (tableau.c_sol[-1] == 0 and tableau.c_sol[:-1] == tableau.b[-1]):
      # This property (true for Dormand-Prince) lets us save a few FLOPs.
      yi = y0 + dt_cast * _weighted_sum(tableau.c_sol, k)

    y1 = tf.identity(yi, name='y1')
    f1 = tf.identity(k[-1], name='f1')
    y1_error = dt_cast * _weighted_sum(tableau.c_error, k)
    return y1, f1, y1_error, k
