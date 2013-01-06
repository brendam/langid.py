#!/usr/bin/env python
"""
train.py - 
Model generator for langid.py
Marco Lui November 2011

Based on research by Marco Lui and Tim Baldwin.

Copyright 2011 Marco Lui <saffsd@gmail.com>. All rights reserved.

Redistribution and use in source and binary forms, with or without modification, are
permitted provided that the following conditions are met:

   1. Redistributions of source code must retain the above copyright notice, this list of
      conditions and the following disclaimer.

   2. Redistributions in binary form must reproduce the above copyright notice, this list
      of conditions and the following disclaimer in the documentation and/or other materials
      provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDER ``AS IS'' AND ANY EXPRESS OR IMPLIED
WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND
FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR
CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF
ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

The views and conclusions contained in the software and documentation are those of the
authors and should not be interpreted as representing official policies, either expressed
or implied, of the copyright holder.
"""

import base64, bz2, pickle
import os, sys, optparse
import array
import numpy as np
import multiprocessing as mp
import tempfile
import marshal
import atexit, shutil
from collections import deque, defaultdict
from contextlib import closing

class Scanner(object):
  alphabet = list(map(chr, list(range(1<<8))))
  """
  Implementation of Aho-Corasick string matching.
  This class should be instantiated with a set of keywords, which
  will then be the only tokens generated by the class's search method,
  """
  def __init__(self, keywords):
    self.build(keywords)

  def __call__(self, value):
    return self.search(value)

  def build(self, keywords):
    goto = dict()
    fail = dict()
    output = defaultdict(set)

    # Algorithm 2
    newstate = 0
    for a in keywords:
      state = 0
      j = 0
      while (j < len(a)) and (state, a[j]) in goto:
        state = goto[(state, a[j])]
        j += 1
      for p in range(j, len(a)):
        newstate += 1
        goto[(state, a[p])] = newstate
        #print "(%d, %s) -> %d" % (state, a[p], newstate)
        state = newstate
      output[state].add(a)
    for a in self.alphabet:
      if (0,a) not in goto: 
        goto[(0,a)] = 0

    # Algorithm 3
    queue = deque()
    for a in self.alphabet:
      if goto[(0,a)] != 0:
        s = goto[(0,a)]
        queue.append(s)
        fail[s] = 0
    while queue:
      r = queue.popleft()
      for a in self.alphabet:
        if (r,a) in goto:
          s = goto[(r,a)]
          queue.append(s)
          state = fail[r]
          while (state,a) not in goto:
            state = fail[state]
          fail[s] = goto[(state,a)]
          #print "f(%d) -> %d" % (s, goto[(state,a)]), output[fail[s]]
          if output[fail[s]]:
            output[s].update(output[fail[s]])

    # Algorithm 4
    self.nextmove = {}
    for a in self.alphabet:
      self.nextmove[(0,a)] = goto[(0,a)]
      if goto[(0,a)] != 0:
        queue.append(goto[(0,a)])
    while queue:
      r = queue.popleft()
      for a in self.alphabet:
        if (r,a) in goto:
          s = goto[(r,a)]
          queue.append(s)
          self.nextmove[(r,a)] = s
        else:
          self.nextmove[(r,a)] = self.nextmove[(fail[r],a)]

    # convert the output to tuples, as tuple iteration is faster
    # than set iteration
    self.output = dict((k, tuple(output[k])) for k in output)

    # Next move encoded as a single array. The index of the next state
    # is located at current state * alphabet size  + ord(c).
    # The choice of 'H' array typecode limits us to 64k states.
    def nextstate_iter():
      for state in range(len(set(self.nextmove.values()))):
        for letter in self.alphabet:
          yield self.nextmove[(state, letter)]
    self.nm_arr = array.array('H', nextstate_iter())

  def __getstate__(self):
    """
    Compiled nextmove and output.
    """
    return (self.nm_arr, self.output)

  def __setstate__(self, value):
    nm_array, output = value
    self.nm_arr = nm_array
    self.output = output
    self.nextmove = {}
    for i, next_state in enumerate(nm_array):
      state = i / 256
      letter = chr(i % 256)
      self.nextmove[(state, letter)] = next_state 

  def search(self, string):
    # TODO: update to using nm_arr
    state = 0
    for letter in string:
      state = self.nextmove[(state, letter)]
      for key in self.output.get(state, []):
        yield key

def chunk(seq, chunksize):
  """
  Break a sequence into chunks not exceeeding a predetermined size
  """
  seq_iter = iter(seq)
  while True:
    chunk = tuple(next(seq_iter) for i in range(chunksize))
    if len(chunk) == 0:
      break
    yield chunk

def offsets(chunks):
  # Work out the path chunk start offsets
  chunk_offsets = [0]
  for c in chunks:
    chunk_offsets.append(chunk_offsets[-1] + len(c))
  return chunk_offsets


def unmarshal_iter(path):
  """
  Open a given path and yield an iterator over items unmarshalled from it.
  """
  with open(path, 'rb') as f:
    while True:
      try:
        yield marshal.load(f)
      except EOFError:
        break

def index(seq):
  """
  Build an index for a sequence of items. Assumes
  that the items in the sequence are unique.
  @param seq the sequence to index
  @returns a dictionary from item to position in the sequence
  """
  return dict((k,v) for (v,k) in enumerate(seq))


def setup_pass1(nm_arr, output_states, state2feat, b_dirs, bucket_map):
  """
  Set the global next-move array used by the aho-corasick scanner
  """
  global __nm_arr, __output_states, __state2feat, __b_dirs, __bucket_map
  __nm_arr = nm_arr
  __output_states = output_states
  __state2feat = state2feat
  __b_dirs = b_dirs
  __bucket_map = bucket_map


def state_trace(path):
  """
  Returns counts of how often each state was entered
  """
  global __nm_arr
  c = defaultdict(int)
  state = 0
  with open(path) as f:
    text = f.read()
    for letter in map(ord,text):
      state = __nm_arr[(state << 8) + letter]
      c[state] += 1
  return c

def pass1(arg):
  """
  Tokenize documents and do counts for each feature
  Split this into buckets chunked over features rather than documents
  """
  global __output_states, __state2feat, __b_dirs, __bucket_map
  chunk_id, chunk_paths = arg
  term_freq = defaultdict(int)
  __procname = mp.current_process().name
  __buckets = [tempfile.mkstemp(prefix=__procname, suffix='.index', dir=p)[0] for p in __b_dirs]

  for doc_id, path in enumerate(chunk_paths):
    count = state_trace(path)
    for state in (set(count) & __output_states):
      for f_id in __state2feat[state]:
        term_freq[doc_id, f_id] += count[state]

  for doc_id, f_id in term_freq:
    bucket_index = __bucket_map[f_id]
    count = term_freq[doc_id, f_id]
    item = ( f_id, chunk_id, doc_id, count )
    os.write(__buckets[bucket_index], marshal.dumps(item))

  for f in __buckets:
    os.close(f)

  return len(term_freq)

def setup_pass2(cm, chunk_offsets, num_instances):
  global __cm, __chunk_offsets, __num_instances
  __cm = cm
  __chunk_offsets = chunk_offsets
  __num_instances = num_instances

def pass2(arg):
  """
  Take a bucket, form a feature map, learn the nb_ptc for it.
  """
  global __cm, __chunk_offsets, __num_instances
  num_feats, base_f_id, b_dir = arg
  fm = np.zeros((__num_instances, num_feats), dtype='int')

  read_count = 0
  for path in os.listdir(b_dir):
    if path.endswith('.index'):
      for f_id, chunk_id, doc_id, count in unmarshal_iter(os.path.join(b_dir, path)):
        doc_index = __chunk_offsets[chunk_id] + doc_id
        f_index = f_id - base_f_id
        fm[doc_index, f_index] = count
        read_count += 1

  prod = np.dot(fm.T, __cm)
  return read_count, prod


def learn_pc(cm):
  """
  @param cm class map
  @returns nb_pc: log(P(C))
  """
  pc = np.log(cm.sum(0))
  nb_pc = array.array('d', pc)
  return nb_pc

def generate_cm(paths, langs):
  num_instances = len(paths)
  num_classes = len(langs)

  # Generate the class map
  lang_index = index(sorted(langs))
  cm = np.zeros((num_instances, num_classes), dtype='bool')
  for docid, path in enumerate(paths):
    lang = os.path.basename(os.path.dirname(path))
    cm[docid, lang_index[lang]] = True
  nb_classes = sorted(lang_index, key=lang_index.get)
  print("generated class map")

  return nb_classes, cm

@atexit.register
def cleanup():
  global b_dirs
  try:
    for d in b_dirs:
      shutil.rmtree(d)
  except NameError:
    # Failed before b_dirs is defined, nothing to clean
    pass

FEATS_PER_CHUNK = 100
def generate_ptc(paths, nb_features, tk_nextmove, state2feat, cm):
  global b_dirs
  num_instances = len(paths)
  num_features = len(nb_features)

  # Generate the feature map
  nm_arr = mp.Array('i', tk_nextmove, lock=False)

  chunk_size = min(len(paths) / (options.job_count*2), 100)
  path_chunks = list(chunk(paths, chunk_size))
  feat_chunks = list(chunk(nb_features, FEATS_PER_CHUNK))

  feat_index = index(nb_features)

  bucket_map = {}
  b_dirs = []
  for chunk_id, feat_chunk in enumerate(feat_chunks):
    for feat in feat_chunk:
      bucket_map[feat_index[feat]] = chunk_id

    b_dirs.append(tempfile.mkdtemp(prefix="train-",suffix="-bucket"))


  output_states = set(state2feat)
  with closing( mp.Pool(options.job_count, setup_pass1, (nm_arr, output_states, state2feat, b_dirs, bucket_map)) 
              ) as pool:
    pass1_out = pool.imap_unordered(pass1, enumerate(path_chunks))
  pool.join()

  write_count = sum(pass1_out)
  print("wrote a total of %d keys" % write_count)

  f_chunk_sizes = list(map(len, feat_chunks))
  f_chunk_offsets = offsets(feat_chunks)
  with closing( mp.Pool(options.job_count, setup_pass2, (cm, offsets(path_chunks), num_instances)) 
              ) as pool:
    pass2_out = pool.imap(pass2, list(zip(f_chunk_sizes, f_chunk_offsets, b_dirs)))
  pool.join()

  reads, pass2_out = list(zip(*pass2_out))
  read_count = sum(reads)

  print("read a total of %d keys (%d short)" % (read_count, write_count - read_count))
  prod = np.vstack(pass2_out)
  ptc = np.log(1 + prod) - np.log(num_features + prod.sum(0))

  nb_ptc = array.array('d')
  for term_dist in ptc.tolist():
    nb_ptc.extend(term_dist)
  return nb_ptc

def read_corpus(path):
  print("data directory: ", path)
  langs = set()
  paths = []
  for dirpath, dirnames, filenames in os.walk(path, followlinks=True):
    for f in filenames:
      paths.append(os.path.join(dirpath, f))
      langs.add(os.path.basename(dirpath))
  print("found %d files" % len(paths))
  print("langs(%d): %s" % (len(langs), sorted(langs)))
  return paths, langs

def build_scanner(nb_features):
  feat_index = index(nb_features)

  # Build the actual scanner
  print("building scanner")
  scanner = Scanner(nb_features)
  tk_nextmove, raw_output = scanner.__getstate__()

  # tk_output is the output function of the scanner. It should generate indices into
  # the feature space directly, as this saves a lookup
  tk_output = {}
  for key in raw_output:
    tk_output[key] = tuple(feat_index[v] for v in raw_output[key])
  
  # Map the scanner raw output directly into feature indexes
  state2feat = {}
  for k,v in list(raw_output.items()):
    state2feat[k] = tuple(feat_index[f] for f in v)
  return tk_nextmove, tk_output, state2feat

if __name__ == "__main__":
  parser = optparse.OptionParser()
  parser.add_option("-o","--output", dest="outfile", help="output model to FILE", metavar="FILE")
  parser.add_option("-c","--corpus", dest="corpus", help="read corpus from DIR", metavar="DIR")
  parser.add_option("-i","--input", dest="infile", help="read features from FILE", metavar="FILE")
  parser.add_option("-j","--jobs", dest="job_count", type="int", help="number of processes to use", default=mp.cpu_count())
  parser.add_option("-t","--temp",dest="temp", help="store temporary files in DIR", metavar="DIR", default=tempfile.gettempdir())
  options, args = parser.parse_args()
  
  tempfile.tempdir = options.temp

  paths, langs = read_corpus(options.corpus)
  nb_features = list(map(eval, open(options.infile)))
  nb_classes, cm = generate_cm(paths, langs)
  tk_nextmove, tk_output, state2feat = build_scanner(nb_features)
  nb_ptc = generate_ptc(paths, nb_features, tk_nextmove, state2feat, cm)
  nb_pc = learn_pc(cm)

  # output the model
  model = nb_ptc, nb_pc, nb_classes, tk_nextmove, tk_output
  string = base64.b64encode(bz2.compress(pickle.dumps(model)))
  with open(options.outfile, 'w') as f:
    f.write(string)
  print("wrote model to %s (%d bytes)" % (options.outfile, len(string)))
