#!/usr/bin/env bash

cd ..

CUDA_VISIBLE_DEVICES=4 python main.py --save 0716_predict_2o_f1 --model DFA --dfa_yaml dfa_2order
