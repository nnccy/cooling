#!/usr/bin/python
# -*- coding:utf8 -*-
import numpy as np
import math
import os
import json

import torch
from torch import nn
from dfa_ode.modules import Classification, Predictor, MLPCell, ODEMergeCell
from common.modules import MSELoss_nan
from torch import nn
from collections import defaultdict


class DFA_ODENets(nn.Module):
    def __init__(self, ode_nums, layers, k_in, k_out, k_state, y_mean, y_std, odes_para, ode_2order, state_transformation_predictor=None,
                 transformations_rules=None, cell_type='cde', linear_decoder=False, Ly_share=False):
        super().__init__()
        self.k_in = k_in
        self.k_out = k_out
        self.k_state = k_state
        self.ode_nums = ode_nums
        self.cell_type = cell_type
        self.linear_decoder = linear_decoder
        self.y_mean = nn.Parameter(torch.FloatTensor(y_mean), requires_grad=False)
        self.y_std = nn.Parameter(torch.FloatTensor(y_std), requires_grad=False)
        if cell_type == 'merge':
            ODECellClass = ODEMergeCell
        # elif cell_type == 'cde':
        #     ODECellClass = CDECell
        else:
            raise NotImplementedError('Cell %s is not implemented' % cell_type)

        Ly = self.make_decoder(k_state, k_out) if Ly_share else None
        self.odes = nn.ModuleList([ODECellClass(
            k_in, k_out, k_state, layers, Ly=(Ly if Ly_share else self.make_decoder(k_state, k_out)),
            ode_2order=ode_2order, name=para['name'], y_type=para['y_type'], cell=para['cell']
        ) for para in odes_para])
        self.transforms = defaultdict(list)
        self.state_transformation_predictor = nn.ModuleDict()
        if state_transformation_predictor is not None:
            for kind, state in state_transformation_predictor:
                if kind == 'predict':
                    self.state_transformation_predictor[str(state)] = Predictor(
                        self.k_state + self.k_state, self.k_state
                    )
                elif kind == 'classify':
                    self.state_transformation_predictor[str(state)] = Classification(
                        self.k_state + self.k_state, self.k_state
                    )
                else:
                    raise NotImplementedError
        if transformations_rules is not None:
            for t in transformations_rules:
                self.add_transform(t['from'], t['to'], t['rules'])

    def make_decoder(self, k_state, k_out):
        if self.linear_decoder:
            Ly = nn.Linear(k_state, k_out)
        else:
            Ly = nn.Sequential(
                nn.Linear(k_state, k_state * 2),
                nn.Tanh(),
                nn.Linear(2 * k_state, k_out)
            )
        return Ly

    def add_transform(self, s1, s2, rules):
        """
        Generating a transformation in DFA
        :param s1:
        :param s2:
        :param rules:
        :return:
        """
        assert 0 <= s1 < self.ode_nums and 0 <= s2 < self.ode_nums
        max_values = torch.nn.Parameter(torch.Tensor([torch.Tensor([float('inf')]) for _ in range(self.k_out)]), requires_grad=False)
        min_values = -torch.nn.Parameter(torch.Tensor([torch.Tensor([float('inf')]) for _ in range(self.k_out)]), requires_grad=False)
        for item, symbol, value in rules:
            assert 0 <= item < self.k_out
            if symbol == 'leq':
                max_values[item] = min(max_values[item], value)
            elif symbol == 'geq':
                min_values[item] = max(min_values[item], value)

        self.transforms[s1].append(
            (min_values, max_values, s2)
        )

    def state_transform(self, state, xt):
        """

        state the diffused states in ODEs  (batch_size, k_state+2)

        :param s1: Current choices of ODEnets (batch_size, 1)
        :param y: outputs (batch_size, k_outs)
        :return: news (batch_size, 1)
        """
        # ht, cum_t, s1 = state[:, :-2], state[:, -2:-1], state[:, -1].long()
        ht, cum_t, s1 = self.select_ht(state), self.select_cum_t(state), self.select_dfa_states(state).squeeze(dim=-1).long()
        new_s = s1.clone().detach()
        new_s_prob = torch.zeros((s1.shape[0], self.ode_nums), device=state.device)
        extra_info = {}
        for state_index in range(self.ode_nums):
            chosen_indices = (s1 == state_index)
            if ~torch.any(chosen_indices):
                continue
            chosen_indices = torch.where((s1 == state_index))[0]
            if str(state_index) in self.state_transformation_predictor.keys():
                predictor = self.state_transformation_predictor[str(state_index)]
                if isinstance(predictor, Predictor):
                    if 'predicted_stop_cum_time' not in extra_info.keys():
                        extra_info['predicted_stop_cum_time'] = torch.zeros_like(cum_t) * float('nan')
                        extra_info['real_cum_time'] = cum_t
                    predicted_cum_t = predictor(ht[chosen_indices], xt[chosen_indices])

                    indices_time_out = (predicted_cum_t.squeeze(dim=-1) <= cum_t[chosen_indices].squeeze(dim=-1))
                    indices_time_out = chosen_indices[indices_time_out]

                    extra_info['predicted_stop_cum_time'][chosen_indices] = predicted_cum_t
                    state_index_plus_one = (state_index + 1) % self.ode_nums
                    if state_index_plus_one == 0:
                        state_index_plus_one = 1

                    new_s[indices_time_out] = state_index_plus_one
                    new_s_prob[indices_time_out, state_index_plus_one] = 1.0
                    # updated_new_s[indices_time_out] = state_index_plus_one
                    # updated_new_s_prob[indices_time_out] = 1.0
                    # new_s[chosen_indices] = updated_new_s
                    # new_s_prob[chosen_indices] =
                    # new_s[chosen_indices[indices_time_out]] = state_index_plus_one
                    # new_s_prob[chosen_indices][indices_time_out][:, state_index_plus_one] = 1.0

                elif isinstance(predictor, Classification):
                    # Applying the classifying network to make multi-classification
                    pred_prob = self.state_transformation_predictor[str(state_index)](
                        torch.cat([ht[chosen_indices], cum_t[chosen_indices]], dim=-1)
                    )
                    pred_label = pred_prob.argmax(dim=-1)
                    new_s[chosen_indices] = pred_label
                    new_s_prob[chosen_indices] = pred_prob
            else:
                # If there is no Classification network in state_transformation_predictor,
                # applying the DFA rules for transformations.
                with torch.no_grad():
                    y = self.decode_y(state) * self.y_std + self.y_mean
                chosen_states = new_s[chosen_indices]
                for min_values, max_values, s2 in self.transforms[state_index]:
                    boolean_results = torch.logical_and(
                        torch.all((y[chosen_indices] < max_values.to(y.device)), dim=-1),
                        torch.all((y[chosen_indices] > min_values.to(y.device)), dim=-1)
                    )
                    if not torch.any(boolean_results):
                        continue
                    updated_states = chosen_states[boolean_results]
                    if not torch.all(updated_states == state_index):
                        conflicted_places = chosen_indices[updated_states != state_index]  # indexes
                        raise AssertionError(
                            'Conflicts in transformation from {} to {} and {}, with current outputs {}'.format(
                                state_index, int(new_s[conflicted_places][0]), s2,
                                y[conflicted_places][0]
                            )
                        )
                    new_s[chosen_indices[boolean_results]] = s2
                    new_s_prob[chosen_indices[boolean_results], s2] = 1.0

        return new_s.unsqueeze(dim=-1), new_s_prob, extra_info

    def decode_y(self, state):
        return state[..., :self.k_out]

    def select_ht(self, state):
        return state[..., self.k_out:-2]

    @staticmethod
    def select_cum_t(state):
        return state[..., -2:-1]

    @staticmethod
    def select_dfa_states(states):
        return states[:, -1:]

    def combinational_ode(self, s, ht, xt, dt):
        nht = torch.zeros_like(ht)
        for i in range(self.ode_nums):
            indices = (s.squeeze(dim=-1) == i)
            if torch.any(indices):
                nht[indices] = self.odes[i](ht[indices], xt[indices], dt[indices])
        return nht

    def forward(self, state, xt, dt, new_s=None):
        """

        :param state: The concatenation of ht, cum_t, cur_s : (bs, k_state + 2)
        :param xt: (bs, k_state)
        :param dt: (bs, 1)
        :param new_s: (bs, 1)
        :return:
        """

        state = torch.zeros((xt.shape[0], self.k_out + self.k_state + 2), device=xt.device) if state is None else state
        ht, cum_t, s = state[..., :-2], state[..., -2:-1], state[..., -1:]
        new_ht = self.combinational_ode(s, ht, xt, dt)
        new_cum_t = cum_t + dt

        if new_s is None:
            new_s, new_s_prob, _ = self.state_transform(
                torch.cat([new_ht, cum_t, s], dim=-1),
                xt
            )
        updated_indices = (s.squeeze(dim=-1) != new_s.squeeze(dim=-1))
        new_cum_t[updated_indices] = 0

        return self.decode_y(new_ht), torch.cat([new_ht, new_cum_t, new_s.float()], dim=-1)


