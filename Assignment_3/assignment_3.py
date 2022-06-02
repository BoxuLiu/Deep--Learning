# -*- coding: utf-8 -*-
"""assignment_3.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1465sbWincp6trA9GPv1dx4OKqZ-v7Rt2
"""

import pickle
import matplotlib.pyplot as plt
import cupy as cp
import numpy as np
import random
import copy
import array as arr

class DataProcess:  
  def LoadBatch(filename, len = None):
    with open(filename, 'rb') as fo:
        dataDict = pickle.load(fo, encoding='bytes')
        X = (dataDict[b"data"] / 255).T
        y = dataDict[b"labels"]
        Y = (cp.eye(10)[y]).T
    return cp.asarray(X[:, :len]), cp.asarray(Y[:, :len]), cp.asarray(y[:len])

  def LoadAllBatch(path, len = None):
    batches = DataProcess.LoadBatch(path + str(1))
    for i in range(2,6):
      batch_tem = DataProcess.LoadBatch(path + str(i))
      batches = (cp.concatenate((batch_tem[0], batches[0]),axis=1), cp.concatenate((batch_tem[1], batches[1]),axis=1), cp.concatenate((batch_tem[2], batches[2]),axis=0))
    batches = [batches[0][:, :len], batches[1][:, :len], batches[2][:len]]
    return batches

  def PreProcess(data):
    for i in range(0, len(data[0, :])):
      data[:, i] = (data[:, i] - cp.mean(data[:, i]))/ cp.std(data[:, i])
    return cp.asarray(data)

  def Flip(pic):
    img_tem = cp.reshape(pic,(3,32,32))
    img_tem = cp.fliplr(img_tem)
    pic = cp.reshape(img_tem,(3072,))
    return pic

  def Move(pic, delta_x = 2, delta_y = 2): 
    pic = cp.reshape(pic,(3,32,32))
    pic = pic[:,:(32-delta_x),:(32-delta_y)]
    tmp = cp.zeros([3,(32-pic.shape[1]),pic.shape[2]])
    pic = cp.concatenate((pic,tmp),axis= 1)
    tmp = cp.zeros([3,32,(32-pic.shape[2])])
    pic = cp.concatenate((tmp,pic),axis= 2)
    pic = cp.reshape(pic,(3072,))
    return pic

  def argumentation(data):
    for i in range(0, data.shape[1]):
      if random.randint(0,1) == 0:
        data[:, i] = DataProcess.Flip(data[:,i])
      if random.randint(0,1) == 0:
        data[:, i] = DataProcess.Move(data[:, i])
    return cp.asarray(data)

  def shuffle(X, Y, y):
    y = cp.reshape(y,(1,len(y)))
    con = cp.concatenate((X, Y),axis=0)
    con = cp.concatenate((con, y),axis=0).T
    cp.random.shuffle(con)
    X = con[:, :3072].T
    Y = con[:, 3072:3082].T
    y = con[:, 3082:].T
    y = cp.reshape(y,(y.shape[1],))
    return X, Y, y

  def split(batch, rate = 0.1):
    train_set = (batch[0][:,0:(1-rate)*batch[0].shape[1]], batch[1][:,0:(1-rate)*batch[1].shape[1]], batch[2][0:(1-rate)*batch[2].shape[0]])
    validation_set = (batch[0][:, (1-rate)*batch[0].shape[1]:], batch[1][:,(1-rate)*batch[1].shape[1]:], batch[2][(1-rate)*batch[2].shape[0]:])
    return train_set, validation_set

class Optimizer:
  def __init__(self):
    pass
  

class Layer:
  def __init__(self, number, activation = "relu", drop_rate = 0):
    self.P = None
    self.G = None
    self.W = None
    self.b = None
    self.number = number
    self.activation = activation

    self.keep_rate = 1 - drop_rate
    self.mask = None
    self.W_mask = None
    self.b_g = None
    self.W_g = None

    self.var = None
    self.mean = None
    self.S = None
    self.S_hat = None
    self.gama = cp.ones((number, 1))
    self.beta = cp.zeros((number, 1))
    self.gama_g = None
    self.beta_g = None
    self.var_av = cp.zeros((number, 1))
    self.mean_av = cp.zeros((number, 1))

    
  def batch_normalize(self, G = None, S = None, mean = None, var = None, alpha = 0.8):
    #Forward
    if G is None:
      #Train
      if mean is None:
        self.var = cp.var(S, axis = 1, keepdims = True)
        self.mean = cp.mean(S, axis = 1, keepdims = True)
        if cp.sum(self.var_av) == 0:
          self.var_av = self.var
          self.mean_av =  self.mean
        else:
          self.var_av = alpha * self.var_av + (1 - alpha) * self.var
          self.mean_av = alpha * self.mean_av + (1 - alpha) * self.mean
        return (S - self.mean) / cp.sqrt(self.var)
      #Test
      else:
        return (S - mean) / cp.sqrt(var + cp.finfo(np.float64).eps)
    
    #Backward
    else:
      var_1 = cp.power(self.var + cp.finfo(np.float64).eps, -0.5)
      var_2 = cp.power(self.var + cp.finfo(np.float64).eps, -1.5)
      G_1 = G * var_1
      G_2 = G * var_2
      D = S - mean
      c = cp.sum(G_2 * D, axis = 1, keepdims = 1)
      return (G_1 - 1/G.shape[1] * cp.sum(G_1, axis = 1, keepdims = True) - 1/G.shape[1] * D * c)
  
  def generate_mask(self, W):
    W_mask = copy.deepcopy(W)
    if self.keep_rate != 1:
        self.mask = cp.random.binomial(1, self.keep_rate, size=[1,W_mask.shape[1]])
        self.mask = self.mask.repeat(W.shape[0],axis=0)
        W_mask *= self.mask
    else:
      self.mask = 1
    return W_mask

  def relu(self, S = None, G_next = None, W_next = None, P = None):
    if S is not None:
      P = copy.deepcopy(S)
      P[P<0] = 0
      return P
    else:
      G = cp.multiply(W_next.T@G_next, P>0)
      return G

  def softmax(self, S = None, Y = None, P = None):
    if S is not None:
      P = cp.exp(S) / cp.sum(cp.exp(S), axis = 0)
      return P
    else:
      G = -(Y - P)
      return G

  def evaluat_classifier(self, X, state = "train", batch_norm = True):
    if state == "train":
      self.W_mask = self.generate_mask(self.W)
      self.S = (self.W_mask@X + self.b) / self.keep_rate
      if batch_norm:
        self.S_hat = self.batch_normalize(S = self.S)
    else:
      self.S = self.W@X + self.b
      if batch_norm:
        self.S_hat = self.batch_normalize(S = self.S, mean = self.mean_av, var = self.var_av)

    if batch_norm:
      S_ = self.S_hat * self.gama + self.beta

    if self.activation == "softmax":
      self.P = self.softmax(S = self.S)
    elif self.activation == "relu":
      if batch_norm:
        self.P = self.relu(S = S_)
      else:
        self.P = self.relu(S = self.S)

  def compute_gradients(self, l, W_next = None, G_next = None, X = None, Y = None, state = "train", batch_norm = True):
    if self.activation == "softmax":
      self.G = self.softmax(Y = Y, P = self.P)
      
    elif self.activation == "relu":
      self.G = self.relu(W_next = W_next, G_next = G_next, P = self.P)
      if batch_norm:
        self.gama_g = 1/X.shape[1] * cp.sum(self.G * self.S_hat, axis = 1, keepdims = True)
        self.beta_g = 1/X.shape[1] * cp.sum(self.G, axis = 1, keepdims = True)
        self.G *= self.gama
        self.G = self.batch_normalize(G = self.G, S = self.S, mean = self.mean, var = self.var)

    self.b_g = cp.reshape(self.G@cp.ones(X.shape[1]) / X.shape[1], (self.G.shape[0],1))
    self.W_g = 1 / X.shape[1] * self.G@X.T + 2 * l * self.W_mask

class FullConnected(Layer):
  def __init__(self, number, act = "relu", drop_rate = 0):
    super(FullConnected, self).__init__(number = number, activation = act, drop_rate = drop_rate)

class NeuralNet():
  def __init__(self, layers = None):
    if layers == None:
      self.layers = []
    else:
      self.layers = layers

    self.data_X = None
    self.data_Y = None
    self.data_y = None
    self.data_X_val = None
    self.data_Y_val = None
    self.data_y_val = None
    self.C = None
    self.G = None
    self.P = None
    self.lamda = None
    self.W_sum = None
    self.accuracy = None
    self.eta = None
    self.epochs = None
    self.batch_size = None
    self.optimizer = None
    self.metrics = None

    self.shuffle = None
    self.argumentation = None
    self.plot = None
    self.batch_norm = None

  def add(self, layer):
    self.layers.append(layer)
  
  def __initial_all(self):
    # self.layers[0].W = cp.random.normal(0, 2/cp.sqrt(self.data_X.shape[0]), (self.layers[0].number, self.data_X.shape[0]))
    # self.layers[0].b = cp.zeros((self.layers[0].number, 1))

    # for i in range(1, len(self.layers)):
    #   self.layers[i].W = cp.random.normal(0, 2/cp.sqrt(self.layers[i-1].number), (self.layers[i].number, self.layers[i-1].number))
    #   self.layers[i].b = cp.zeros((self.layers[i].number, 1))

    self.layers[0].W = cp.random.normal(0, 1e-4, (self.layers[0].number, self.data_X.shape[0]))
    self.layers[0].b = cp.zeros((self.layers[0].number, 1))

    for i in range(1, len(self.layers)):
      self.layers[i].W = cp.random.normal(0, 1e-4, (self.layers[i].number, self.layers[i-1].number))
      self.layers[i].b = cp.zeros((self.layers[i].number, 1))
      
  def compile(self, optimizer = Optimizer(), metrics = None):
    self.optimizer = optimizer
    self.metrics = metrics

  
  def compute_cost(self, X, Y, batch_norm = True):
    self.__forward(X, state = "compute_cost", batch_norm = batch_norm)
    self.W_sum = 0
    for i in range(0, len(self.layers)):
      self.W_sum += cp.sum(self.layers[i].W**2)

    loss = -cp.sum(Y*cp.log(self.P))/X.shape[1]
    cost = loss + self.lamda * self.W_sum

    return loss, cost


  def compute_accuracy(self, X, y, batch_norm = True):
    self.__forward(X, state = "compute_accuracy", batch_norm = batch_norm)
    argMaxP = cp.argmax(self.P, axis=0)
    return argMaxP.T[argMaxP == cp.asarray(y)].shape[0] / y.shape[0]
    
  def __forward(self, X, state = "train", batch_norm = True):
    self.layers[0].evaluat_classifier(X, state, batch_norm = batch_norm)

    for i in range(1, len(self.layers)):
      self.layers[i].evaluat_classifier(self.layers[i-1].P, state, batch_norm = batch_norm)
    self.P = self.layers[-1].P

  def __backward(self, X, Y, l, batch_norm = True):
    self.layers[-1].compute_gradients(l, X = self.layers[-2].P, Y = Y, batch_norm = batch_norm)
    for i in range(len(self.layers) - 2, 0, -1):
      self.layers[i].compute_gradients(l, X = self.layers[i-1].P, W_next = self.layers[i+1].W, G_next = self.layers[i+1].G, batch_norm = batch_norm)
    
    self.layers[0].compute_gradients(l, X = X, W_next = self.layers[1].W, G_next = self.layers[1].G, batch_norm = batch_norm)

  def fit(self, data, eta = 0.001, lamda = 0.01, epochs = 10, batch_size = 100, batch_norm = True, 
          split_rate = 0.02, preprocess = False, argumentation = False, shuffle = True,
          plot = False):
    if preprocess:
      data[0] = DataProcess.PreProcess(data[0])
    data_train, data_val = DataProcess.split(data, split_rate)
    self.data_X = data_train[0]
    self.data_Y = data_train[1]
    self.data_y = data_train[2]
    self.data_X_val = data_val[0]
    self.data_Y_val = data_val[1]
    self.data_y_val = data_val[2]
    self.lamda = lamda
    self.eta = eta
    self.epochs = epochs
    self.batch_size = batch_size
    self.shuffle = shuffle
    self.argumentation = argumentation
    self.plot = plot
    self.batch_norm = batch_norm
    self.__initial_all()

  def mini_batch_GD(self):
    count = 0

    cost_training = []
    cost_validation = []
    loss_training = []
    loss_validation = []
    accuracy_training = []
    accuracy_validation = []

    if self.optimizer != None:
      eta_max = 1e-1
      eta_min = 1e-5
      n_s = 2250
      t = 0
      k = self.data_X.shape[1] / self.batch_size * self.epochs / 5
      self.eta = eta_min
    for i in range(self.epochs):
      if self.plot:
        print("epoch:", i+1)
      if self.shuffle:
        self.data_X, self.data_Y, self.data_y = DataProcess.shuffle(self.data_X, self.data_Y, self.data_y)
      if self.argumentation:
        data_X = DataProcess.argumentation(data_X)
      
      for j in range(1, int(self.data_X.shape[1]/self.batch_size) + 1):
        # if self.optimizer != None:
        #   pass
        j_start = (j - 1) * self.batch_size
        j_end = j * self.batch_size
        batch_X = self.data_X[:, j_start:j_end]
        batch_Y = self.data_Y[:, j_start:j_end]
        self.__forward(batch_X, batch_norm = self.batch_norm)
        self.__backward(batch_X, batch_Y, self.lamda, batch_norm = self.batch_norm)
        for layer in range(0, len(self.layers)):
          self.layers[layer].W -= self.eta * self.layers[layer].W_g * self.layers[layer].mask
          self.layers[layer].b -= self.eta * self.layers[layer].b_g
        
        if self.batch_norm:
          for layer in range(0, len(self.layers)-1):
            self.layers[layer].gama -= self.eta * self.layers[layer].gama_g
            self.layers[layer].beta -= self.eta * self.layers[layer].beta_g
        

        count += 1
        if self.optimizer != None:
          if t < n_s:
            self.eta += ((eta_max - eta_min)/n_s)
          elif t > n_s:
            self.eta -= ((eta_max - eta_min)/n_s)
          else:
            # eta_max = np.exp(-count/k+np.log(0.1-1e-5))+eta_min
            self.eta -= ((eta_max - eta_min)/n_s)

          t = (t + 1) % (2 * n_s)
        if self.plot and count % 50 == 0:
          loss_t, cost_t = self.compute_cost(self.data_X, self.data_Y, batch_norm = self.batch_norm)
          loss_v, cost_v = self.compute_cost(self.data_X_val, self.data_Y_val, batch_norm = self.batch_norm)

          accuracy_t = self.compute_accuracy(self.data_X, self.data_y, batch_norm = self.batch_norm)
          accuracy_v = self.compute_accuracy(self.data_X_val, self.data_y_val, batch_norm = self.batch_norm)

          cost_training.append(cost_t)
          cost_validation.append(cost_v)
          loss_training.append(loss_t)
          loss_validation.append(loss_v)
          accuracy_training.append(accuracy_t)
          accuracy_validation.append(accuracy_v)

    if self.plot:
      cost_training = cp.array(cost_training)
      cost_validation = cp.array(cost_validation)
      loss_training = cp.array(loss_training)
      loss_validation = cp.array(loss_validation)
      accuracy_training = cp.array(accuracy_training)
      accuracy_validation = cp.array(accuracy_validation)

      print("training accuracy:", accuracy_training[-1])
      print("validation accuracy:", accuracy_validation[-1])
      self.PlotPerformance(count/50, cp.asnumpy(cost_training), cp.asnumpy(cost_validation), "Cost")
      self.PlotPerformance(count/50, cp.asnumpy(loss_training), cp.asnumpy(loss_validation), "Loss")
      self.PlotPerformance(count/50, cp.asnumpy(accuracy_training), cp.asnumpy(accuracy_validation), "Accuracy")
    else:
      return   self.compute_accuracy(self.data_X, self.data_y, batch_norm = self.batch_norm), self.compute_accuracy(self.data_X_val, self.data_y_val, batch_norm = self.batch_norm)


  def start(self):
    return self.mini_batch_GD()
    
  def PlotPerformance(self, n_epochs, costs_training, costs_val, title = ""):
    epochs = cp.asnumpy(cp.arange(n_epochs))

    fig, ax = plt.subplots(figsize=(10, 8))
    ax.plot(epochs, costs_training, label="Training set")
    ax.plot(epochs, costs_val, label="Validation set")
    ax.legend()
    ax.set(xlabel='Update step', ylabel=title)
    ax.grid()   

  def show(self):
    for layer in self.layers:
      print(layer.number)

  def montage(self, W):
    fig, ax = plt.subplots(2,5)
    for i in range(2):
      for j in range(5):
        im  = W[i*5+j,:].reshape(32,32,3, order='F')
        sim = (im-cp.min(im[:]))/(cp.max(im[:])-cp.min(im[:]))
        sim = sim.transpose(1,0,2)
        ax[i][j].imshow(sim, interpolation='nearest')
        ax[i][j].set_title("y="+str(5*i+j))
        ax[i][j].axis('off')
    plt.show()

def GridSearchNum(l_max, l_min, num = 8):
    l = []
    range_l = l_max - l_min
    for i in range(num):
      l.append(10**l_min)
      l_min += (range_l)/(num-1)
    return l

def RandomSearchNum(l_max, l_min, num = 8):
    l = []
    for i in range(num):
      l.append(random.uniform(l_max, l_min))
    return l

def SearchL(l):
    accuracy = []
    for i in range(len(l)):
      print(i+1, "th accuracy:")
      network.fit(data, batch_size=100, epochs=20, eta = 0.001, lamda = l[i], batch_norm = True,
            split_rate = 0.1, preprocess = False, argumentation = False, shuffle = True,
            plot = False)
      
      train, validation = network.start()
      accuracy.append((l[i], train, validation))
    return accuracy

def AutoRun(times = 10, epochs = 10, batch_size = 100, l = GridSearchNum(-1, -5)[5], plot = False, argumentation = False):
    train_accuracy = []
    validation_accuracy = []
    for i in range(times):
      network.fit(data, batch_size = batch_size, split_rate = 0.02, 
            epochs=epochs, eta = 0.001, lamda = l, 
            preprocess = False, argumentation = argumentation, plot = plot, shuffle = True)

      train, validation = network.start()
      train_accuracy.append(train)
      validation_accuracy.append(validation)
    print("training accuracy average:", np.mean(train))
    print("test accuracy average:", np.mean(validation))

data = DataProcess.LoadAllBatch('/content/drive/MyDrive/DirName/Datasets/cifar-10-batches-py/data_batch_')
data[0] = DataProcess.PreProcess(data[0])

network = NeuralNet()
network.add(FullConnected(50, "relu", drop_rate = 0))
network.add(FullConnected(50, "relu", drop_rate = 0))
# network.add(FullConnected(20, "relu", drop_rate = 0))
# network.add(FullConnected(20, "relu", drop_rate = 0))
# network.add(FullConnected(10, "relu", drop_rate = 0))
# network.add(FullConnected(10, "relu", drop_rate = 0))
# network.add(FullConnected(10, "relu", drop_rate = 0))
# network.add(FullConnected(10, "relu", drop_rate = 0))
network.add(FullConnected(10, "softmax"))
# network.show()

network.compile(Optimizer())

network.fit(data, batch_size=100, epochs=20, eta = 0.001, lamda = 0.007196856730011514, batch_norm = False,
            split_rate = 0.1, preprocess = False, argumentation = False, shuffle = True,
            plot = True)

network.start()

accuracy_grid = SearchL(GridSearchNum(0, -3))
for i in range(len(accuracy_grid)):
  print(accuracy_grid[i])

accuracy_grid = SearchL(RandomSearchNum(0.0026826957952797246, 0.019306977288832485))
for i in range(len(accuracy_grid)):
  print(accuracy_grid[i])