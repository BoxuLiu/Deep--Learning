# -*- coding: utf-8 -*-
"""Untitled1.ipynb

Automatically generated by Colaboratory.

Original file is located at
    https://colab.research.google.com/drive/1AmqQu018-dZP_R1mr4FBgKg3Vzw0ZB2N
"""

import pickle
import matplotlib.pyplot as plt
import cupy as np
import random

def LoadBatch(filename, len = None):
    with open(filename, 'rb') as fo:
        dataDict = pickle.load(fo, encoding='bytes')
        X = (dataDict[b"data"] / 255).T
        y = dataDict[b"labels"]
        Y = (np.eye(10)[y]).T
    # return X[:, :len], Y[:, :len], y[:len]
    return [np.asarray(X[:, :len]), np.asarray(Y[:, :len]), np.asarray(y[:len])]

def PreProcess(data):
    for i in range(0, len(data[0, :])):
      data[:, i] = (data[:, i] - np.mean(data[:, i]))/ np.std(data[:, i])
    # return data
    return np.asarray(data)

def Argumentation(data):
    for i in range(0, len(data[0, :])):
      if random.randint(0,9) >=5:
        img_tem = np.reshape(data[:,i],(3,32,32))
        img_tem = np.fliplr(img_tem)
        data[:, i] = np.reshape(img_tem,(3072,))
    return np.asarray(data)


def InitialParameters(data, lables, num):
    W_0 = np.random.normal(0, 1/np.sqrt(data.shape[0]), (num, data.shape[0]))
    b_0 = np.zeros((num, 1))
    W_1 = np.random.normal(0, 1/np.sqrt(num), (len(lables), num))
    b_1 = np.zeros((len(lables), 1))
    W = [W_0, W_1]
    b = [b_0, b_1]
    return W, b

def EvaluateClassifier(X, W, b, func = "relu"):
    s = W@X + b
    if func == "softmax":
      p = np.exp(s) / np.sum(np.exp(s), axis = 0)
      return p
    elif func == "relu":
      s[s<0] = 0
      return s
    elif func == "sigmod":
      p = np.exp(s) / (np.exp(s) + 1)
      return p

def Forward(data, W, b):
  output = EvaluateClassifier(data, W[0], b[0], "relu")
  p = EvaluateClassifier(output, W[1], b[1], "softmax")
  return output, p

def ComputeCost(X, Y, W, b, l, func = "softmax"):
    _,p = Forward(X, W, b)
    # p = EvaluateClassifier(X, W, b, func)
    if func == "softmax":
      loss = -np.sum(Y*np.log(p))/X.shape[1]
      c = loss + l * (np.sum(W[0]**2) + np.sum(W[1]**2))
      return c, loss
      # return c
    elif func == "sigmod":
      c = -np.sum((1-Y)*np.log(1-p) + Y*np.log(p))/Y.shape[0]/X.shape[1]


def ComputeAccuracy(X, y, W, b, func = "softmax"):
    _,p = Forward(X, W, b)
    argMaxP = np.argmax(p, axis=0)
    acc = argMaxP.T[argMaxP == np.asarray(y)].shape[0] / X.shape[1]
    return acc

def ComputeGradients(X, Y, P, W, l, G = None, W_next = None, func = "softmax", P_pre = None):
    if func == "softmax":
      G = -(Y - P)
      b_g = np.reshape(G@np.ones(X.shape[1]) / X.shape[1], (Y.shape[0],1))
      W_g = 1 / X.shape[1] * G@X.T + 2 * l * W
    elif func == "sigmod":
      G = (P - Y) / Y.shape[0]
      b_g = np.reshape(G@np.ones(X.shape[1]) / X.shape[1], (Y.shape[0],1))
      W_g = 1 / X.shape[1] * G@X.T + 2 * l * W
    elif func == "relu":
      G = np.multiply(W_next.T@G, P_pre>0)
      b_g = np.reshape(G@np.ones(X.shape[1]) / X.shape[1], (G.shape[0],1))
      W_g = 1 / X.shape[1] * G@X.T + 2 * l * W
      
    return W_g, b_g, G

def ComputeGradsNum(X, Y, P, W, b, lamda, h = 1e-6):
    no  =  W[1].shape[0]
    d  =  X.shape[0]
    grad_W = np.zeros(W[1].shape);
    grad_b = np.zeros((no, 1));
    c = ComputeCost(X, Y, W[1], b[1], lamda);
    for i in range(len(b[1])):
      b_try = np.array(b[1])
      b_try[i] += h
      c2 = ComputeCost(X, Y, W[1], b_try, lamda)
      grad_b[i] = (c2-c) / h
    for i in range(W[1].shape[0]):
      for j in range(W[1].shape[1]):
        W_try = np.array(W[1])
        W_try[i,j] += h
        c2 = ComputeCost(X, Y, W_try, b[1], lamda)
        grad_W[i,j] = (c2-c) / h
    return [grad_W, grad_b]

def Backward(data, Y, P_1, W, l, P_0):
  W_g_2, b_g_2, G_2 = ComputeGradients(P_0, Y, P_1, W[1], l, func = "softmax")
  W_g_1, b_g_1, G_1 = ComputeGradients(data, Y, P_1, W[0], l, W_next = W[1], func = "relu", G = G_2, P_pre = P_0)
  
  return [W_g_1, W_g_2], [b_g_1, b_g_2]

def shuffle(X, Y, y):
    y = np.reshape(y,(1,len(y)))
    con = np.concatenate((X, Y),axis=0)
    con = np.concatenate((con, y),axis=0).T
    np.random.shuffle(con)
    X = con[:, :3072].T
    Y = con[:, 3072:3082].T
    y = con[:, 3082:].T
    y = np.reshape(y,(y.shape[1],))
    return X, Y, y

def PlotPerformance(n_epochs, costs_training, costs_val, title = ""):
    epochs = np.asnumpy(np.arange(n_epochs))

    fig, ax = plt.subplots(figsize=(10, 8))
    ax.plot(epochs, costs_training, label="Training set")
    ax.plot(epochs, costs_val, label="Validation set")
    ax.legend()
    ax.set(xlabel='Update step', ylabel=title)
    ax.grid()

    plt.savefig("/content/drive/MyDrive/DirName/Result Pics/" + title + ".png", bbox_inches="tight")


def MiniBatchGD(X, Y, W, b, l = 0.01, y = None, n_batches =
 100, eta = 0.001, epochs = 10, X_validation = None, Y_validation = None, y_validation = None, eta_max = 1e-1, eta_min = 1e-5, n_s = 816):
    cost_training = []
    cost_validation = []

    loss_training = []
    loss_validation = []

    accuracy_training = []
    accuracy_validation = []

    t = 0
    count = 0
    eta = eta_min
    for i in range(epochs):
      X, Y, y = shuffle(X, Y, y)
      print("epoch:", i+1)
      for j in range(1, int(X.shape[1]/n_batches) + 1):
        j_start = (j - 1) * n_batches
        j_end = j * n_batches
        batch_x = X[:, j_start:j_end]
        batch_y = Y[:, j_start:j_end]
        p_0,p_1 = Forward(batch_x, W, b)
        W_g, b_g = Backward(batch_x, batch_y, p_1, W, l, p_0)
        W[0] -= eta * W_g[0]
        b[0] -= eta * b_g[0]
        W[1] -= eta * W_g[1]
        b[1] -= eta * b_g[1]

        if t < n_s:
          eta += ((eta_max - eta_min)/n_s)
        elif t >= n_s:
          eta -= ((eta_max - eta_min)/n_s)

        t = (t + 1) % (2 * n_s)
        
        count += 1


        cost_t, loss_t = ComputeCost(X, Y, W, b, l)
        cost_v, loss_v = ComputeCost(X_validation, Y_validation, W, b, l)

        accuracy_t = ComputeAccuracy(X, y, W, b)
        accuracy_v = ComputeAccuracy(X_validation, y_validation, W, b)

        cost_training.append(cost_t)
        cost_validation.append(cost_v)
        loss_training.append(loss_t)
        loss_validation.append(loss_v)
        accuracy_training.append(accuracy_t)
        accuracy_validation.append(accuracy_v)

    cost_training = np.array(cost_training)
    cost_validation = np.array(cost_validation)
    loss_training = np.array(loss_training)
    loss_validation = np.array(loss_validation)
    accuracy_training = np.array(accuracy_training)
    accuracy_validation = np.array(accuracy_validation)

    # return   ComputeAccuracy(X, y, W, b), ComputeAccuracy(X_validation, y_validation, W, b)
    print("training accuracy:", ComputeAccuracy(X, y, W, b))
    print("validation accuracy:", ComputeAccuracy(X_validation, y_validation, W, b))
    PlotPerformance(count, np.asnumpy(cost_training), np.asnumpy(cost_validation), "Cost")
    PlotPerformance(count, np.asnumpy(loss_training), np.asnumpy(loss_validation), "Loss")
    PlotPerformance(count, np.asnumpy(accuracy_training), np.asnumpy(accuracy_validation), "Accuracy")



def montage(W):
    fig, ax = plt.subplots(2,5)
    for i in range(2):
      for j in range(5):
        im  = W[i*5+j,:].reshape(32,32,3, order='F')
        sim = (im-np.min(im[:]))/(np.max(im[:])-np.min(im[:]))
        sim = sim.transpose(1,0,2)
        ax[i][j].imshow(sim, interpolation='nearest')
        ax[i][j].set_title("y="+str(5*i+j))
        ax[i][j].axis('off')
    plt.show()

def LoadAllBatch(path, len = None):
    batches = LoadBatch(path + str(1))
    for i in range(2,6):
      batch_tem = LoadBatch(path + str(i))
      batches = (np.concatenate((batch_tem[0], batches[0]),axis=1), np.concatenate((batch_tem[1], batches[1]),axis=1), np.concatenate((batch_tem[2], batches[2]),axis=0))
    batches = [batches[0][:, :len], batches[1][:, :len], batches[2][:len]]
    return batches

def split(batch, rate = 0.1):
    train_set = (batch[0][:,0:(1-rate)*batch[0].shape[1]], batch[1][:,0:(1-rate)*batch[1].shape[1]], batch[2][0:(1-rate)*batch[2].shape[0]])
    validation_set = (batch[0][:, (1-rate)*batch[0].shape[1]:], batch[1][:,(1-rate)*batch[1].shape[1]:], batch[2][(1-rate)*batch[2].shape[0]:])
    return train_set, validation_set

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
      print("第", i+1, "个accuracy:")
      train, validation = MiniBatchGD(train_set[0], train_set[1], W, b, y = train_set[2], X_validation = validation_set[0], Y_validation = validation_set[1], y_validation = validation_set[2], l = l[i])
      accuracy.append((l[i], train, validation))
    return accuracy

train_set = LoadAllBatch('/content/drive/MyDrive/DirName/Datasets/cifar-10-batches-py/data_batch_')
test_set = LoadBatch('/content/drive/MyDrive/DirName/Datasets/cifar-10-batches-py/test_batch')
test_set[0] = PreProcess(test_set[0])
train_set[0] = PreProcess(train_set[0])
train_set, validation_set = split(train_set, 0.02)
W, b = InitialParameters(train_set[0],train_set[1],50)

MiniBatchGD(train_set[0], train_set[1], W, b, y = train_set[2], X_validation = validation_set[0], Y_validation = validation_set[1], y_validation = validation_set[2], l = GridSearchNum(-1, -5)[5])

accuracy_grid = SearchL(GridSearchNum(-1, -5))
for i in range(len(accuracy_grid)):
  print(accuracy_grid[i])
print(train_set[0].shape)

accuracy_random = SearchL(RandomSearchNum(accuracy_grid[4][0]+0.003, accuracy_grid[6][0]-0.01, 30))
for i in range(len(accuracy_random)):
  print(accuracy_random[i])

cost, loss = ComputeCost(test_set[0], test_set[1], W, b, GridSearchNum(-1, -5)[5])
print("test cost:", cost)
print("test loss:", loss)
print("test accuracy:", ComputeAccuracy(test_set[0], test_set[2], W, b))