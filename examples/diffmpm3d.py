import taichi_lang as ti
import os
import math
import numpy as np
import random
import cv2
import matplotlib.pyplot as plt
import time
import taichi as tc

real = ti.f32
ti.set_default_fp(real)

dim = 3
# this will be overwritten
n_particles = 0
n_solid_particles = 0
n_actuators = 0
n_grid = 128
dx = 1 / n_grid
inv_dx = 1 / dx
dt = 1e-3
p_vol = 1
E = 10
# TODO: update
mu = E
la = E
max_steps = 1024
steps = 1024
gravity = 3.8
target = [0.8, 0.2, 0.2]

scalar = lambda: ti.var(dt=real)
vec = lambda: ti.Vector(dim, dt=real)
mat = lambda: ti.Matrix(dim, dim, dt=real)

actuator_id = ti.global_var(ti.i32)
particle_type = ti.global_var(ti.i32)
x, v = vec(), vec()
grid_v_in, grid_m_in = vec(), scalar()
grid_v_out = vec()
C, F = mat(), mat()

screen = ti.Vector(3, dt=real)

loss = scalar()

n_sin_waves = 4
weights = scalar()
bias = scalar()
x_avg = vec()

actuation = scalar()
actuation_omega = 20
act_strength = 4

# ti.cfg.arch = ti.x86_64
# ti.cfg.use_llvm = True
ti.cfg.arch = ti.cuda


# ti.cfg.print_ir = True


visualize_resolution = 1024

@ti.layout
def place():
  ti.root.dense(ti.ij, (n_actuators, n_sin_waves)).place(weights)
  ti.root.dense(ti.i, n_actuators).place(bias)

  ti.root.dense(ti.ij, (max_steps, n_actuators)).place(actuation)
  ti.root.dense(ti.i, n_particles).place(actuator_id, particle_type)
  ti.root.dense(ti.l, max_steps).dense(ti.k, n_particles).place(x, v, C, F)
  ti.root.dense(ti.ijk, n_grid).place(grid_v_in, grid_m_in, grid_v_out)
  ti.root.place(loss, x_avg)
  ti.root.dense(ti.ij, (visualize_resolution, visualize_resolution)).place(screen)

  ti.root.lazy_grad()


def zero_vec():
  return [0.0, 0.0, 0.0]


def zero_matrix():
  return [zero_vec(), zero_vec(), zero_vec()]


@ti.kernel
def clear_grid():
  for i, j, k in grid_m_in:
    grid_v_in[i, j, k] = [0, 0, 0]
    grid_m_in[i, j, k] = 0
    grid_v_in.grad[i, j, k] = [0, 0, 0]
    grid_m_in.grad[i, j, k] = 0
    grid_v_out.grad[i, j, k] = [0, 0, 0]


@ti.kernel
def clear_particle_grad():
  # for all time steps and all particles
  for f, i in x:
    x.grad[f, i] = zero_vec()
    v.grad[f, i] = zero_vec()
    C.grad[f, i] = zero_matrix()
    F.grad[f, i] = zero_matrix()


@ti.kernel
def clear_actuation_grad():
  for t, i in actuation:
    actuation[t, i] = 0.0


@ti.kernel
def p2g(f: ti.i32):
  for p in range(0, n_particles):
    base = ti.cast(x[f, p] * inv_dx - 0.5, ti.i32)
    fx = x[f, p] * inv_dx - ti.cast(base, ti.i32)
    w = [0.5 * ti.sqr(1.5 - fx), 0.75 - ti.sqr(fx - 1),
         0.5 * ti.sqr(fx - 0.5)]
    new_F = (ti.Matrix.diag(dim=dim, val=1) + dt * C[f, p]) @ F[f, p]
    J = ti.determinant(new_F)
    if particle_type[p] == 0:  # fluid
      sqrtJ = ti.sqrt(J)
      # TODO: need pow(x, 1/3)
      new_F = ti.Matrix([[sqrtJ, 0, 0], [0, sqrtJ, 0], [0, 0, 1]])

    F[f + 1, p] = new_F
    # r, s = ti.polar_decompose(new_F)

    act_id = actuator_id[p]

    act = actuation[f, ti.max(0, act_id)] * act_strength
    if act_id == -1:
      act = 0.0
    # ti.print(act)

    A = ti.Matrix([[0.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 0.0]]) * act
    cauchy = ti.Matrix(zero_matrix())
    mass = 0.0
    if particle_type[p] == 0:
      mass = 4
      cauchy = ti.Matrix([[1.0, 0.0, 0.0], [0.0, 0.1, 0.0], [0.0, 0.0, 1.0]]) * (J - 1) * E
    else:
      pass
      # mass = 1
      # cauchy = 2 * mu * (new_F - r) @ ti.transposed(new_F) + \
      #          ti.Matrix.diag(2, la * (J - 1) * J)
    cauchy += new_F @ A @ ti.transposed(new_F)
    stress = -(dt * p_vol * 4 * inv_dx * inv_dx) * cauchy
    affine = stress + mass * C[f, p]
    for i in ti.static(range(3)):
      for j in ti.static(range(3)):
        for k in ti.static(range(3)):
          offset = ti.Vector([i, j, k])
          dpos = (ti.cast(ti.Vector([i, j, k]), real) - fx) * dx
          weight = w[i](0) * w[j](1) * w[k](2)
          grid_v_in[base + offset].atomic_add(
            weight * (mass * v[f, p] + affine @ dpos))
          grid_m_in[base + offset].atomic_add(weight * mass)


bound = 3
coeff = 0.0


@ti.kernel
def grid_op():
  for i, j, k in grid_m_in:
    inv_m = 1 / (grid_m_in[i, j, k] + 1e-10)
    v_out = inv_m * grid_v_in[i, j, k]
    v_out[1] -= dt * gravity

    if i < bound and v_out[0] < 0:
      v_out[0] = 0
      v_out[1] = 0
      v_out[2] = 0
    if i > n_grid - bound and v_out[0] > 0:
      v_out[0] = 0
      v_out[1] = 0
      v_out[2] = 0

    if k < bound and v_out[2] < 0:
      v_out[0] = 0
      v_out[1] = 0
      v_out[2] = 0
    if k > n_grid - bound and v_out[2] > 0:
      v_out[0] = 0
      v_out[1] = 0
      v_out[2] = 0

    if j < bound and v_out[1] < 0:
      v_out[0] = 0
      v_out[1] = 0
      v_out[2] = 0
      normal = ti.Vector([0.0, 1.0, 0.0])
      lsq = ti.sqr(normal).sum()
      if lsq > 0.5:
        if ti.static(coeff < 0):
          v_out[0] = 0
          v_out[1] = 0
          v_out[2] = 0
        else:
          lin = (ti.transposed(v_out) @ normal)(0)
          if lin < 0:
            vit = v_out - lin * normal
            lit = vit.norm() + 1e-10
            if lit + coeff * lin <= 0:
              v_out[0] = 0
              v_out[1] = 0
              v_out[2] = 0
            else:
              v_out = (1 + coeff * lin / lit) * vit
    if j > n_grid - bound and v_out[1] > 0:
      v_out[0] = 0
      v_out[1] = 0
      v_out[2] = 0

    grid_v_out[i, j, k] = v_out


@ti.kernel
def g2p(f: ti.i32):
  for p in range(0, n_particles):
    base = ti.cast(x[f, p] * inv_dx - 0.5, ti.i32)
    fx = x[f, p] * inv_dx - ti.cast(base, real)
    w = [0.5 * ti.sqr(1.5 - fx), 0.75 - ti.sqr(fx - 1.0),
         0.5 * ti.sqr(fx - 0.5)]
    new_v = ti.Vector(zero_vec())
    new_C = ti.Matrix(zero_matrix())

    for i in ti.static(range(3)):
      for j in ti.static(range(3)):
        for k in ti.static(range(3)):
          dpos = ti.cast(ti.Vector([i, j, k]), real) - fx
          g_v = grid_v_out[base(0) + i, base(1) + j, base(2) + k]
          weight = w[i](0) * w[j](1) * w[k](2)
          new_v += weight * g_v
          new_C += 4 * weight * ti.outer_product(g_v, dpos) * inv_dx

    v[f + 1, p] = new_v
    x[f + 1, p] = x[f, p] + dt * v[f + 1, p]
    C[f + 1, p] = new_C


@ti.kernel
def compute_actuation(t: ti.i32):
  for i in range(n_actuators):
    act = 0.0
    for j in ti.static(range(n_sin_waves)):
      act += weights[i, j] * ti.sin(
        actuation_omega * t * dt + 2 * math.pi / n_sin_waves * j)
    act += bias[i]
    actuation[t, i] = ti.tanh(act)


@ti.kernel
def compute_x_avg():
  for i in range(n_particles):
    contrib = 0.0
    if particle_type[i] == 1:
      contrib = 1.0 / n_solid_particles
    x_avg[None].atomic_add(contrib * x[steps - 1, i])


@ti.kernel
def compute_loss():
  dist = x_avg[None][0]
  loss[None] = -dist


def forward(total_steps=steps):
  # simulation
  for s in range(total_steps - 1):
    clear_grid()
    compute_actuation()
    p2g(s)
    grid_op()
    g2p(s)

  x_avg[None] = [0, 0, 0]
  compute_x_avg()
  compute_loss()
  return loss[None]


def backward():
  clear_particle_grad()

  compute_loss.grad()
  compute_x_avg.grad()
  for s in reversed(range(steps - 1)):
    # Since we do not store the grid history (to save space), we redo p2g and grid op
    clear_grid()
    p2g(s)
    grid_op()

    g2p.grad(s)
    grid_op.grad()
    p2g.grad(s)
    compute_actuation.grad()


class Scene:
  def __init__(self):
    self.n_particles = 0
    self.n_solid_particles = 0
    self.x = []
    self.actuator_id = []
    self.particle_type = []
    self.offset_x = 0
    self.offset_y = 0
    self.offset_z = 0

  def add_rect(self, x, y, z, w, h, d, actuation, ptype=1):
    if ptype == 0:
      assert actuation == -1
    global n_particles
    w_count = int(w / dx) * 2
    h_count = int(h / dx) * 2
    d_count = int(d / dx) * 2
    real_dx = w / w_count
    real_dy = h / h_count
    real_dz = d / d_count
    for i in range(w_count):
      for j in range(h_count):
        for k in range(d_count):
          self.x.append([x + (i + 0.5) * real_dx + self.offset_x,
                         y + (j + 0.5) * real_dy + self.offset_y,
                         z + (k + 0.5) * real_dz + self.offset_z])
          self.actuator_id.append(actuation)
          self.particle_type.append(ptype)
          self.n_particles += 1
          self.n_solid_particles += int(ptype == 1)
          if self.n_particles % 1000 == 0:
            print("num particles", self.n_particles)

  def set_offset(self, x, y):
    self.offset_x = x
    self.offset_y = y

  def finalize(self):
    global n_particles, n_solid_particles
    n_particles = self.n_particles
    n_solid_particles = max(self.n_solid_particles, 1)
    print('n_particles', n_particles)
    print('n_solid', n_solid_particles)

  def set_n_actuators(self, n_act):
    global n_actuators
    n_actuators = n_act


def fish(scene):
  scene.add_rect(0.025, 0.025, 0.95, 0.1, -1, ptype=0)
  scene.add_rect(0.1, 0.2, 0.15, 0.05, -1)
  scene.add_rect(0.1, 0.15, 0.025, 0.05, 0)
  scene.add_rect(0.125, 0.15, 0.025, 0.05, 1)
  scene.add_rect(0.2, 0.15, 0.025, 0.05, 2)
  scene.add_rect(0.225, 0.15, 0.025, 0.05, 3)
  scene.set_n_actuators(4)


def robot(scene):
  scene.set_offset(0.1, 0.03)
  scene.add_rect(0.0, 0.1, 0.3, 0.1, -1)
  scene.add_rect(0.0, 0.0, 0.05, 0.1, 0)
  scene.add_rect(0.05, 0.0, 0.05, 0.1, 1)
  scene.add_rect(0.2, 0.0, 0.05, 0.1, 2)
  scene.add_rect(0.25, 0.0, 0.05, 0.1, 3)
  scene.set_n_actuators(4)


gui = tc.core.GUI("Differentiable MPM", tc.Vectori(1024, 1024))
canvas = gui.get_canvas()

@ti.kernel
def splat(t: ti.i32):
  for i in range(n_particles):
    pos = ti.cast(x[t, i] * visualize_resolution, ti.i32)
    screen[pos[0], pos[1]][0] += 0.1

res = [visualize_resolution, visualize_resolution]

@ti.kernel
def copy_back_and_clear(img: np.ndarray):
  for i in range(res[0]):
    for j in range(res[1]):
      coord = ((res[1] - 1 - j) * res[0] + i) * 3
      for c in ti.static(range(3)):
        img[coord + c] = screen[i, j][2 - c]
        screen[i, j][2 - c] = 0


def main():
  tc.set_gdb_trigger()
  # initialization
  scene = Scene()
  # fish(scene)
  # robot(scene)
  scene.add_rect(0.4, 0.4, 0.2, 0.1, 0.3, 0.1, -1, 0)
  scene.finalize()

  for i in range(n_actuators):
    for j in range(n_sin_waves):
      weights[i, j] = np.random.randn() * 0.01

  for i in range(scene.n_particles):
    x[0, i] = scene.x[i]
    F[0, i] = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
    actuator_id[i] = scene.actuator_id[i]
    particle_type[i] = scene.particle_type[i]

  vec = tc.Vector
  losses = []
  for iter in range(100):
    ti.clear_all_gradients()
    l = forward()
    losses.append(l)
    loss.grad[None] = 1
    backward()
    print('i=', iter, 'loss=', l)
    learning_rate = 0.1

    for i in range(n_actuators):
      for j in range(n_sin_waves):
        # print(weights.grad[i, j])
        weights[i, j] -= learning_rate * weights.grad[i, j]
      bias[i] -= learning_rate * bias.grad[i]

    if iter % 10 == 0:
      # visualize
      print(1)
      forward()
      print(2)
      for s in range(63, steps, 16):
        print(s)
        img = np.zeros((res[1] * res[0] * 3,), dtype=np.float32)
        splat(s)
        copy_back_and_clear(img)
        img = img.reshape(res[1], res[0], 3)
        img = np.sqrt(img)
        cv2.imshow('img', img)
        cv2.waitKey(1)

  # ti.profiler_print()
  plt.title("Optimization of Initial Velocity")
  plt.ylabel("Loss")
  plt.xlabel("Gradient Descent Iterations")
  plt.plot(losses)
  plt.show()


if __name__ == '__main__':
  main()
