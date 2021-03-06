import argparse
import io
import json
import logging
import xml.etree.ElementTree as ET
from copy import deepcopy
from functools import reduce
from itertools import groupby
from pathlib import Path
from typing import List, NamedTuple, Sequence

import gensim.downloader
import networkx as nx
import networkx.algorithms.components
import numpy as np
from scipy.spatial import distance
from sklearn.metrics import silhouette_score
from sklearn_extra.cluster import KMedoids
from tqdm import tqdm

from udpipe_model import UDPipeModel

MIN_CLUSTER_SIZE = 50
NODE_DISTANCE_THRESHOLD = 0.3


class Reltuple(NamedTuple):
    left_arg: str
    left_arg_lemmas: str
    left_w2v: np.ndarray
    relation: str
    relation_lemmas: str
    right_arg: str
    right_arg_lemmas: str
    right_deprel: str
    right_w2v: np.ndarray


class SentenceReltuples:
    def __init__(self, sentence, w2v_model, additional_relations=False, stopwords=[]):
        self.sentence = sentence
        self.sentence_vector = _get_phrase_vector(sentence, "all", w2v_model)
        self._stopwords = set(stopwords)
        words_ids_tuples = self._get_words_ids_tuples(
            additional_relations=additional_relations
        )
        self._reltuples = [self._to_tuple(t, w2v_model) for t in words_ids_tuples]
        self._reltuples = [
            reltuple
            for reltuple in self._reltuples
            if reltuple.left_arg != reltuple.right_arg
        ]
        logging.info(
            "{} relations were extracted from the sentence {}:\n".format(
                len(self._reltuples), self.sentence.getText()
            )
            + "\n".join(
                "({}, {}, {})".format(
                    reltuple.left_arg, reltuple.relation, reltuple.right_arg
                )
                for reltuple in self._reltuples
            )
        )

    def __getitem__(self, index):
        return self._reltuples[index]

    def _to_tuple(self, reltuple, w2v_model):
        left_arg = self._arg_to_string(reltuple[0], lemmatized=False)
        left_arg_lemmas = self._arg_to_string(reltuple[0], lemmatized=True)
        left_w2v = _get_phrase_vector(self.sentence, reltuple[0], w2v_model)

        relation = self._relation_to_string(reltuple[1])
        relation_lemmas = self._relation_to_string(reltuple[1], lemmatized=True)

        right_arg = self._arg_to_string(reltuple[2], lemmatized=False)
        right_arg_lemmas = self._arg_to_string(reltuple[2], lemmatized=True)
        right_deprel = self.sentence.words[self._get_root(reltuple[2]).id].deprel
        right_w2v = _get_phrase_vector(self.sentence, reltuple[2], w2v_model)

        return Reltuple(
            left_arg,
            left_arg_lemmas,
            left_w2v,
            relation,
            relation_lemmas,
            right_arg,
            right_arg_lemmas,
            right_deprel,
            right_w2v,
        )

    def _relation_to_string(self, relation, lemmatized=False):
        if isinstance(relation, list) and not lemmatized:
            string_ = " ".join(self.sentence.words[id_].form for id_ in relation)
        elif isinstance(relation, list) and lemmatized:
            string_ = " ".join(self.sentence.words[id_].lemma for id_ in relation)
        elif isinstance(relation, str):
            string_ = relation
        else:
            raise TypeError
        return self._clean_string(string_)

    def _arg_to_string(self, words_ids, lemmatized=False):
        if lemmatized:
            string_ = " ".join(
                self.sentence.words[id_].lemma.strip() for id_ in words_ids
            )
        else:
            string_ = " ".join(
                self.sentence.words[id_].form.strip() for id_ in words_ids
            )
        return self._clean_string(string_)

    @staticmethod
    def _clean_string(string_):
        res = (
            "".join(
                char
                for char in string_
                if char.isalnum() or char.isspace() or char in ",.;-—_/:%"
            )
            .lower()
            .strip(" .,:;-")
        )
        return res

    def _get_words_ids_tuples(self, additional_relations=False):
        result = []
        for word in self.sentence.words:
            if word.deprel == "cop":
                result += self._get_copula_reltuples(word)
            elif word.upostag == "VERB":
                result += self._get_verb_reltuples(word)
        if additional_relations:
            args = {tuple(left_arg) for left_arg, _, _ in result} | {
                tuple(right_arg) for _, _, right_arg in result
            }
            for arg in args:
                result += self._get_additional_reltuples(list(arg))
        return [
            (left_arg, relation, right_arg)
            for left_arg, relation, right_arg in result
            if not self._is_stopwords(left_arg) and not self._is_stopwords(right_arg)
        ]

    def _get_verb_reltuples(self, verb):
        for child_id in verb.children:
            child = self.sentence.words[child_id]
            if child.deprel == "xcomp":
                return ()
        subjects = self._get_subjects(verb)
        right_args = self._get_right_args(verb)
        return [
            (subj, self._get_relation(verb, right_arg=arg), arg)
            for subj in subjects
            for arg in right_args
        ]

    def _get_copula_reltuples(self, copula):
        right_arg = self._get_right_args(copula)[0]
        parent = self.sentence.words[copula.head]
        subjects = self._get_subjects(parent)
        relation = self._get_copula(copula)
        return [(subj, relation, right_arg) for subj in subjects]

    def _get_additional_reltuples(self, words_ids):
        result = []
        is_a_deprels = ["appos", "flat", "flat:foreign", "flat:name", "conj"]
        relates_to_deprels = ["nmod"]
        main_phrase_ids = words_ids
        root = self._get_root(words_ids)
        children_ids = [id_ for id_ in words_ids if id_ in root.children]

        for child_id in children_ids:
            child = self.sentence.words[child_id]
            if child.deprel in is_a_deprels:
                subtree = self._get_subtree(child)
                descendants_ids = [id_ for id_ in words_ids if id_ in subtree]
                result.append((words_ids, "_is_a_", descendants_ids))
                result += self._get_additional_reltuples(descendants_ids)
                main_phrase_ids = [
                    id_ for id_ in main_phrase_ids if id_ not in descendants_ids
                ]
        if len(words_ids) != len(main_phrase_ids):  # found "is_a" relation?
            result.append((words_ids, "_is_a_", main_phrase_ids))
            result += self._get_additional_reltuples(main_phrase_ids)
            return result

        old_main_phrase_length = len(main_phrase_ids)
        for child_id in children_ids:
            child = self.sentence.words[child_id]
            if child.deprel in relates_to_deprels:
                subtree = self._get_subtree(child)
                descendants_ids = [id_ for id_ in words_ids if id_ in subtree]
                result.append((words_ids, "_relates_to_", descendants_ids))
                result += self._get_additional_reltuples(descendants_ids)
                main_phrase_ids = [
                    id_ for id_ in main_phrase_ids if id_ not in descendants_ids
                ]
        if old_main_phrase_length != len(
            main_phrase_ids
        ):  # found "relates_to" relation?
            result.append((words_ids, "_is_a_", main_phrase_ids))
            result += self._get_additional_reltuples(main_phrase_ids)
        elif len(main_phrase_ids) > 1:
            result.append((main_phrase_ids, "_is_a_", [root.id]))
        return result

    def _get_relation(self, word, right_arg=None):
        prefix = self._get_relation_prefix(word)
        postfix = self._get_relation_postfix(word, right_arg=right_arg)
        relation = prefix + [word.id] + postfix
        return relation

    def _get_relation_prefix(self, relation):
        prefix = []
        for child_id in relation.children:
            child = self.sentence.words[child_id]
            if (
                child.deprel == "case"
                or child.deprel == "aux"
                or child.deprel == "aux:pass"
                or child.upostag == "PART"
            ) and child.id < relation.id:
                prefix.append(child.id)
        parent = self.sentence.words[relation.head]
        if relation.deprel == "xcomp":
            prefix = self._get_relation(parent) + prefix
        if self._is_conjunct(relation) and parent.deprel == "xcomp":
            grandparent = self.sentence.words[parent.head]
            prefix = self._get_relation(grandparent) + prefix
        return prefix

    def _get_relation_postfix(self, relation, right_arg=None):
        postfix = []
        for child_id in relation.children:
            child = self.sentence.words[child_id]
            if (
                child.deprel == "case"
                or child.deprel == "aux"
                or child.deprel == "aux:pass"
                or child.upostag == "PART"
            ) and child.id > relation.id:
                postfix.append(child.id)
        if right_arg:
            case_id = self._get_first_case(right_arg)
            if case_id is not None:
                postfix.append(case_id)
                right_arg.remove(case_id)
        return postfix

    def _get_right_args(self, word):
        if word.deprel == "cop":
            args_list = self._get_copula_right_args(word)
        else:
            args_list = self._get_verb_right_args(word)
        return args_list

    def _get_copula_right_args(self, word):
        parent = self.sentence.words[word.head]
        words_ids = self._get_subtree(parent)
        copulas = self._get_all_copulas(parent)
        for copula_words_ids in copulas:
            for id_ in copula_words_ids:
                words_ids.remove(id_)
        subjects = self._get_subjects(parent)
        for subj in subjects:
            for id_to_remove in subj:
                try:
                    words_ids.remove(id_to_remove)
                except ValueError:
                    continue
        return [words_ids]

    def _get_verb_right_args(self, word):
        args_list = []
        for child_id in word.children:
            child = self.sentence.words[child_id]
            if self._is_right_arg(child):
                args_list.append(self._get_subtree(child))
        parent = self.sentence.words[word.head]
        if word.deprel == "xcomp":
            args_list += self._get_verb_right_args(parent)
        if self._is_conjunct(word) and parent.deprel == "xcomp":
            grandparent = self.sentence.words[parent.head]
            args_list += self._get_verb_right_args(grandparent)
        return args_list

    def _get_subjects(self, word):
        subj_list = []
        for child_id in word.children:
            child = self.sentence.words[child_id]
            if self._is_subject(child):
                subj_list.append(self._get_subtree(child))
        if not subj_list and (word.deprel == "conj" or word.deprel == "xcomp"):
            parent = self.sentence.words[word.head]
            subj_list = self._get_subjects(parent)
        return subj_list

    def _get_subtree(self, word):
        if not list(word.children):
            return [word.id]
        res_ids = []
        for child_id in (id for id in word.children if id < word.id):
            child = self.sentence.words[child_id]
            res_ids.extend(self._get_subtree(child))
        res_ids.append(word.id)
        for child_id in (id for id in word.children if id > word.id):
            child = self.sentence.words[child_id]
            res_ids.extend(self._get_subtree(child))
        return res_ids

    def _get_first_case(self, words_ids):
        root = self._get_root(words_ids)
        for id_ in words_ids:
            word = self.sentence.words[id_]
            if id_ < root.id and word.deprel == "case":
                return id_
        return None

    def _get_copula(self, word):
        parent = self.sentence.words[word.head]
        part_ids = []
        for sibling_id in parent.children:
            sibling = self.sentence.words[sibling_id]
            if sibling.id == word.id:
                return part_ids + [sibling.id]
            if sibling.upostag == "PART":
                part_ids.append(sibling.id)
            else:
                part_ids = []
        return []

    def _get_all_copulas(self, word):
        res = []
        for child_id in word.children:
            child = self.sentence.words[child_id]
            if child.deprel == "cop":
                res.append(self._get_copula(child))
        return res

    def _get_root(self, words_ids):
        root = None
        for id_ in words_ids:
            word = self.sentence.words[id_]
            if word.head not in words_ids:
                root = word
        return root

    def _is_stopwords(self, words_ids):
        return {self.sentence.words[id_].lemma for id_ in words_ids}.issubset(
            self._stopwords
        ) or (
            len(words_ids) == 1
            and len(self.sentence.words[words_ids[0]].lemma) == 1
            and self.sentence.words[words_ids[0]].lemma.isalpha()
        )

    @staticmethod
    def _is_subject(word):
        return word.deprel in ("nsubj", "nsubj:pass")

    @staticmethod
    def _is_right_arg(word):
        return word.deprel in ("obj", "iobj", "obl", "obl:agent", "iobl")

    @staticmethod
    def _is_conjunct(word):
        return word.deprel == "conj"


class RelGraph:
    def __init__(self):
        self._graph = nx.MultiDiGraph()

    @classmethod
    def from_reltuples_iter(cls, reltuples_iter: Sequence[SentenceReltuples]):
        graph = cls()
        for sentence_reltuple in reltuples_iter:
            graph.add_sentence_reltuples(sentence_reltuple)

    @property
    def nodes_number(self):
        return self._graph.number_of_nodes()

    @property
    def edges_number(self):
        return self._graph.number_of_edges()

    def add_sentence_reltuples(
        self, sentence_reltuples: SentenceReltuples, cluster: int = 0
    ):
        sentence_text = sentence_reltuples.sentence.getText()
        for reltuple in sentence_reltuples:
            source = self._add_node(
                reltuple.left_arg_lemmas,
                sentence_text,
                label=reltuple.left_arg,
                vector=reltuple.left_w2v,
                feat_type=cluster,
            )
            target = self._add_node(
                reltuple.right_arg_lemmas,
                sentence_text,
                label=reltuple.right_arg,
                vector=reltuple.right_w2v,
                feat_type=cluster,
            )
            self._add_edge(
                source,
                target,
                reltuple.relation,
                reltuple.relation_lemmas,
                reltuple.right_deprel,
                sentence_text,
                feat_type=cluster,
            )
        self._inherit_relations()

    def merge_relations(self):
        while True:
            same_name_nodes_to_merge_lists = self._find_same_name_nodes_to_merge()
            if len(same_name_nodes_to_merge_lists) > 0:
                for same_name_nodes_to_merge in same_name_nodes_to_merge_lists:
                    logging.info(
                        (
                            "Found {n_to_merge} same name arguments to merge:\n"
                            "{args}\n"
                            "Clusters of arguments: \n"
                            "{clusters}"
                        ).format(
                            n_to_merge=len(same_name_nodes_to_merge),
                            args="\n".join(
                                self._graph.nodes[node]["label"]
                                for node in same_name_nodes_to_merge
                            ),
                            clusters="\n".join(
                                str(self._graph.nodes[node]["feat_type"])
                                for node in same_name_nodes_to_merge
                            ),
                        )
                    )
                    self._merge_nodes(same_name_nodes_to_merge)

            nodes_to_merge = []
            edges_to_merge = []

            for source, target, key in self._graph.edges:
                targets_to_merge = self._find_nodes_to_merge(source=source, key=key)
                if len(targets_to_merge) > 1:
                    logging.info(
                        (
                            "Found {n_to_merge} right arguments to merge: \n"
                            "Shared left argument: {left_arg} \n"
                            "Shared relation: {rel} \n"
                            "Values to merge: \n"
                            "{values_to_merge}"
                        ).format(
                            n_to_merge=len(targets_to_merge),
                            left_arg=self._graph.nodes[source]["label"],
                            rel=self._graph[source][next(iter(targets_to_merge))][key][
                                "label"
                            ],
                            values_to_merge="\n".join(
                                self._graph.nodes[node]["label"]
                                for node in targets_to_merge
                            ),
                        )
                    )
                    nodes_to_merge = targets_to_merge
                    break

                sources_to_merge = self._find_nodes_to_merge(target=target, key=key)
                if len(sources_to_merge) > 1:
                    logging.info(
                        (
                            "Found {n_to_merge} left arguments to merge: \n"
                            "Shared right argument: {right_arg} \n"
                            "Shared relation: {rel} \n"
                            "Values to merge: \n"
                            "{values_to_merge}"
                        ).format(
                            n_to_merge=len(sources_to_merge),
                            right_arg=self._graph.nodes[target]["label"],
                            rel=self._graph[next(iter(sources_to_merge))][target][key][
                                "label"
                            ],
                            values_to_merge="\n".join(
                                self._graph.nodes[node]["label"]
                                for node in sources_to_merge
                            ),
                        )
                    )
                    nodes_to_merge = sources_to_merge
                    break

                edges_to_merge = self._find_edges_to_merge(source, target)
                if len(edges_to_merge) > 1:
                    logging.info(
                        (
                            "Found {n_to_merge} relations to merge: \n"
                            "Shared left argument: {left_arg} \n"
                            "Shared right argument: {right_arg} \n"
                            "Values to merge: \n"
                            "{values_to_merge}"
                        ).format(
                            n_to_merge=len(edges_to_merge),
                            left_arg=self._graph.nodes[source]["label"],
                            right_arg=self._graph.nodes[target]["label"],
                            values_to_merge="\n".join(
                                {
                                    self._graph[s][t][key]["label"]
                                    for s, t, key in edges_to_merge
                                }
                            ),
                        )
                    )
                    break

            if len(nodes_to_merge) > 1:
                self._merge_nodes(nodes_to_merge)
            elif len(edges_to_merge) > 1:
                self._merge_edges(edges_to_merge)
            else:
                break

    def filter_nodes(self, n_nodes_to_leave):
        nodes_to_remove = self._find_nodes_to_remove(n_nodes_to_leave)
        self._perform_filtering(nodes_to_remove)

    def _add_edge(
        self, source, target, label, lemmas, deprel, description, weight=1, feat_type=0
    ):
        if label in ["_is_a_", "_relates_to_"]:
            key = label
        else:
            key = "{} + {}".format(lemmas, deprel)
        if isinstance(description, str):
            description = set([description])
        else:
            description = set(description)
        if isinstance(feat_type, int):
            feat_type = set([feat_type])
        else:
            feat_type = set(feat_type)
        if not self._graph.has_edge(source, target, key=key):
            if label == "_is_a_":
                self._graph.add_edge(
                    source,
                    target,
                    key=key,
                    label=label,
                    lemmas=lemmas,
                    deprel=deprel,
                    description=description,
                    weight=weight,
                    feat_type=feat_type,
                    viz={"color": {"b": 255, "g": 0, "r": 0}},
                )
            elif label == "_relates_to_":
                self._graph.add_edge(
                    source,
                    target,
                    key=key,
                    label=label,
                    lemmas=lemmas,
                    deprel=deprel,
                    description=description,
                    weight=weight,
                    feat_type=feat_type,
                    viz={"color": {"b": 0, "g": 255, "r": 0}},
                )
            else:
                self._graph.add_edge(
                    source,
                    target,
                    key=key,
                    label=label,
                    lemmas=lemmas,
                    deprel=deprel,
                    description=description,
                    weight=weight,
                    feat_type=feat_type,
                )
        else:
            # this edge already exists
            self._graph[source][target][key]["description"] = (
                description | self._graph[source][target][key]["description"]
            )
            self._graph[source][target][key]["feat_type"] = (
                feat_type | self._graph[source][target][key]["feat_type"]
            )
            self._graph[source][target][key]["weight"] += weight

    def _add_node(self, lemmas, description, label, weight=1, vector=None, feat_type=0):
        if isinstance(description, str):
            description = set([description])
        else:
            description = set(description)
        if isinstance(feat_type, int):
            feat_type = set([feat_type])
        else:
            feat_type = set(feat_type)
        node = "{} + {}".format(lemmas, str(feat_type))
        if node not in self._graph:
            self._graph.add_node(
                node,
                lemmas=lemmas,
                label=label,
                description=description,
                weight=weight,
                vector=vector,
                feat_type=feat_type,
            )
        else:
            # this node already exists
            self._graph.nodes[node]["label"] = " | ".join(
                set(self._graph.nodes[node]["label"].split(" | ") + label.split(" | "))
            )
            self._graph.nodes[node]["description"] = (
                description | self._graph.nodes[node]["description"]
            )
            self._graph.nodes[node]["feat_type"] = (
                feat_type | self._graph.nodes[node]["feat_type"]
            )
            self._graph.nodes[node]["vector"] = (
                self._graph.nodes[node]["vector"] + vector
            ) / 2
            self._graph.nodes[node]["weight"] += weight
        return node

    def _inherit_relations(self):
        modified = True
        while modified:
            modified = False
            for node in self._graph:
                predecessors_by_is_a = {
                    n
                    for n in self._graph.predecessors(node)
                    if self._graph.has_edge(n, node, key="_is_a_")
                }
                in_verb_rel_edges = [
                    (source, key, attr)
                    for n in predecessors_by_is_a
                    for source, _, key, attr in self._graph.in_edges(
                        n, data=True, keys=True
                    )
                    if key not in ["_is_a_", "_relates_to_"]
                ]
                out_verb_rel_edges = [
                    (target, key, attr)
                    for n in predecessors_by_is_a
                    for _, target, key, attr in self._graph.out_edges(
                        n, data=True, keys=True
                    )
                    if key not in ["_is_a_", "_relates_to_"]
                ]
                for source, key, attr in in_verb_rel_edges:
                    if self._graph.has_edge(source, node, key=key):
                        continue
                    self._add_edge(
                        source,
                        node,
                        attr["label"],
                        attr["lemmas"],
                        attr["deprel"],
                        attr["description"],
                        weight=attr["weight"],
                        feat_type=attr["feat_type"],
                    )
                    modified = True
                for target, key, attr in out_verb_rel_edges:
                    if self._graph.has_edge(node, target, key=key):
                        continue
                    self._add_edge(
                        node,
                        target,
                        attr["label"],
                        attr["lemmas"],
                        attr["deprel"],
                        attr["description"],
                        weight=attr["weight"],
                        feat_type=attr["feat_type"],
                    )
                    modified = True

    def _find_target_merge_candidates(self, source, key):
        return {
            target
            for target in self._graph.successors(source)
            if self._graph.has_edge(source, target, key=key)
            and self._graph[source][target][key]["label"]
            not in ["_is_a_", "_relates_to_"]
            and (
                self._graph.nodes[source]["feat_type"]
                & self._graph.nodes[target]["feat_type"]
            )
        }

    def _find_source_merge_candidates(self, target, key):
        return {
            source
            for source in self._graph.predecessors(target)
            if self._graph.has_edge(source, target, key=key)
            and self._graph[source][target][key]["label"]
            not in ["_is_a_", "_relates_to_"]
            and (
                self._graph.nodes[source]["feat_type"]
                & self._graph.nodes[target]["feat_type"]
            )
        }

    def _filter_node_merge_candidates(self, nodes):
        res = nodes.copy()
        for node1 in res.copy():
            for node2 in res.difference([node1]):
                if self._graph.has_edge(node1, node2) or (
                    self._graph.nodes[node1]["description"]
                    & self._graph.nodes[node2]["description"]
                ):
                    res.discard(node1)
                    res.discard(node2)

        if len(res) < 2:
            return res

        main_node, *other_nodes = sorted(
            res,
            key=lambda node: (self._graph.nodes[node]["weight"], node),
            reverse=True,
        )
        for node in other_nodes:
            if self._nodes_distance(main_node, node) > NODE_DISTANCE_THRESHOLD:
                res.discard(node)
        return res

    def _nodes_distance(self, node1, node2):
        vector1: np.ndarray = self._graph.nodes[node1]["vector"]
        vector2: np.ndarray = self._graph.nodes[node2]["vector"]
        if not vector1.any() or not vector2.any():
            return float("inf")
        else:
            return float(distance.cosine(vector1, vector2))

    def _find_nodes_to_merge(self, source=None, target=None, key=None):
        if source is not None and key is not None:
            res = self._find_target_merge_candidates(source, key)
        elif target is not None and key is not None:
            res = self._find_source_merge_candidates(target, key)
        else:
            raise ValueError("Wrong set of specified arguments")

        if len(res) < 2:
            return res

        res = self._filter_node_merge_candidates(res)
        return res

    def _find_edges_to_merge(self, source, target):
        keys = [
            (key, cluster, attr["label"])
            for _, _, key, attr in self._graph.out_edges(source, keys=True, data=True)
            if self._graph.has_edge(source, target, key=key)
            and attr["label"] not in ["_is_a_", "_relates_to_"]
            for cluster in attr["feat_type"]
        ]

        keys.sort(key=lambda elem: elem[1:])
        skip_cluster = False
        for cluster_name, cluster_group in groupby(keys, key=lambda elem: elem[1]):
            cluster_group_list = list(cluster_group)
            if len(cluster_group_list) == 1:
                continue
            for _, label_group in groupby(cluster_group_list, key=lambda elem: elem[2]):
                if len(list(label_group)) > 1:
                    skip_cluster = True
                    break
            if skip_cluster:
                skip_cluster = False
                continue
            else:
                keys = set(key for key, *_ in cluster_group_list)
                cluster = cluster_name
                break
        else:  # all clusters have been skipped
            return set()

        edges = set()
        for s, t, key, feat_type in self._graph.edges(keys=True, data="feat_type"):
            if key in keys and cluster in feat_type:
                edges.add((s, t, key))

        # relations from the same sentence are out
        for s1, t1, key1 in edges.copy():
            for s2, t2, key2 in edges.copy():
                if (s1, t1, key1) != (s2, t2, key2) and (
                    self._graph.edges[s1, t1, key1]["description"]
                    & self._graph.edges[s2, t2, key2]["description"]
                ):
                    edges.discard((s1, t1, key1))
                    edges.discard((s2, t2, key2))
        return edges

    def _find_same_name_nodes_to_merge(self):
        labels_edges_dict = {}
        for s, t, k in self._graph.edges:
            labels = (
                self._graph.nodes[s]["label"],
                self._graph[s][t][k]["label"],
                self._graph.nodes[t]["label"],
            )
            if labels not in labels_edges_dict:
                labels_edges_dict[labels] = [(s, t, k)]
            else:
                labels_edges_dict[labels].append((s, t, k))

        res = []
        seen_nodes = set()
        for edge_list in labels_edges_dict.values():
            if len(edge_list) > 1:
                sources = frozenset(s for s, _, _ in edge_list if s not in seen_nodes)
                targets = frozenset(t for _, t, _ in edge_list if t not in seen_nodes)
                if len(sources) > 1:
                    res.append(sources)
                    seen_nodes.update(sources | targets)
                if len(targets) > 1:
                    res.append(targets)
                    seen_nodes.update(sources | targets)
        return res

    def _merge_nodes(self, nodes):
        main_node, *other_nodes = sorted(
            nodes,
            key=lambda node: (self._graph.nodes[node]["weight"], node),
            reverse=True,
        )

        feat_type = self._graph.nodes[main_node]["feat_type"]
        for node in other_nodes:
            feat_type |= self._graph.nodes[node]["feat_type"]
        for node in other_nodes:
            self._add_node(
                self._graph.nodes[main_node]["lemmas"],
                self._graph.nodes[node]["description"],
                label=self._graph.nodes[node]["label"],
                weight=self._graph.nodes[node]["weight"],
                vector=self._graph.nodes[node]["vector"],
                feat_type=feat_type,
            )

        for source, target, key in self._graph.edges(other_nodes, keys=True):
            edge_ends = None
            if source in other_nodes:  # "out" edge
                edge_ends = (main_node, target)
            elif target in other_nodes:  # "in" edge
                edge_ends = (source, main_node)
            self._add_edge(
                *edge_ends,
                self._graph.edges[source, target, key]["label"],
                self._graph.edges[source, target, key]["lemmas"],
                self._graph.edges[source, target, key]["deprel"],
                self._graph.edges[source, target, key]["description"],
                weight=self._graph.edges[source, target, key]["weight"],
                feat_type=feat_type,
            )

        for node in other_nodes:
            self._graph.remove_node(node)

    def _merge_edges(self, edges):
        def new_str_attr_value(attr_key):
            return " | ".join(
                reduce(
                    lambda x, y: x | y,
                    (
                        set(self._graph[source][target][key][attr_key].split(" | "))
                        for source, target, key in edges
                    ),
                )
            )

        new_label = new_str_attr_value("label")
        new_lemmas = new_str_attr_value("lemmas")
        new_deprel = new_str_attr_value("deprel")
        new_weight = sum(
            (
                self._graph[source][target][key]["weight"]
                for source, target, key in edges
            )
        )
        for source, target, key in edges:
            self._add_edge(
                source,
                target,
                new_label,
                new_lemmas,
                new_deprel,
                self._graph[source][target][key]["description"],
                weight=new_weight,
                feat_type=self._graph[source][target][key]["feat_type"],
            )
            self._graph.remove_edge(source, target, key=key)

    def save(self, path):
        self._transform()
        for node in self._graph:
            if self._graph.nodes[node].get("vector") is not None:
                self._graph.nodes[node]["vector"] = str(
                    self._graph.nodes[node]["vector"].tolist()
                )
            self._graph.nodes[node]["description"] = " | ".join(
                self._graph.nodes[node]["description"]
            )
            self._graph.nodes[node]["feat_type"] = " | ".join(
                str(elem) for elem in self._graph.nodes[node]["feat_type"]
            )
        stream_buffer = io.BytesIO()
        nx.write_gexf(self._graph, stream_buffer, encoding="utf-8", version="1.1draft")
        xml_string = stream_buffer.getvalue().decode("utf-8")
        root_element = ET.fromstring(xml_string)
        self._fix_gexf(root_element)
        ET.register_namespace("", "http://www.gexf.net/1.1draft")
        xml_tree = ET.ElementTree(root_element)
        xml_tree.write(path, encoding="utf-8")

    def _find_nodes_to_remove(self, n_nodes_to_leave):
        all_nodes = sorted(
            set(self._graph.nodes),
            key=lambda node: self._graph.nodes[node]["weight"],
            reverse=True,
        )
        nodes_to_leave = set(all_nodes[: min(n_nodes_to_leave, len(all_nodes))])
        next_node_index = min(n_nodes_to_leave, len(all_nodes)) + 1

        # exclude nodes connected by additional relations only
        while True:
            for node in nodes_to_leave:
                if all(
                    [
                        self._graph.edges[source, target, key]["label"]
                        in ["_is_a_", "_relates_to_"]
                        for source, target, key in self._graph.out_edges(
                            node, keys=True
                        )
                        if target in nodes_to_leave
                    ]
                    + [
                        self._graph.edges[source, target, key]["label"]
                        in ["_is_a_", "_relates_to_"]
                        for source, target, key in self._graph.in_edges(node, keys=True)
                        if source in nodes_to_leave
                    ]
                ):
                    nodes_to_leave.discard(node)
                    if next_node_index < len(all_nodes):
                        nodes_to_leave.add(all_nodes[next_node_index])
                        next_node_index += 1
                    break
            else:
                break

        return set(all_nodes) - set(nodes_to_leave)

    def _perform_filtering(self, nodes_to_remove):
        nodes_to_remove = set(nodes_to_remove)
        while True:
            for node in nodes_to_remove:
                in_edges = list(self._graph.in_edges(node, keys=True))
                out_edges = list(self._graph.out_edges(node, keys=True))
                for pred, _, key_pred in in_edges:
                    for _, succ, key_succ in out_edges:
                        if (
                            self._graph[node][succ][key_succ]["label"]
                            != self._graph[pred][node][key_pred]["label"]
                        ):
                            continue
                        # FIXME wrong attrs in the new edge?
                        # to implement A->B->C  ==>  A->C
                        self._add_edge(
                            pred,
                            succ,
                            self._graph[node][succ][key_succ]["label"],
                            self._graph[node][succ][key_succ]["lemmas"],
                            self._graph[node][succ][key_succ]["deprel"],
                            self._graph[node][succ][key_succ]["description"],
                            weight=self._graph[node][succ][key_succ]["weight"],
                            feat_type=self._graph[node][succ][key_succ]["feat_type"],
                        )
                self._graph.remove_node(node)
                nodes_to_remove.discard(node)
                break
            else:
                break

    def _transform(self):
        # transform relations from edges to nodes with specific node_type and color
        for node in self._graph:
            self._graph.nodes[node]["node_type"] = "argument"
        for source, target, key, attr in list(self._graph.edges(data=True, keys=True)):
            node = "{}({}; {})".format(
                self._graph.edges[source, target, key]["label"], source, target
            )
            new_attr = deepcopy(attr)
            if self._graph.edges[source, target, key]["label"] == "_is_a_":
                new_attr["viz"] = {"color": {"b": 160, "g": 160, "r": 255}}
            elif self._graph.edges[source, target, key]["label"] == "_relates_to_":
                new_attr["viz"] = {"color": {"b": 160, "g": 255, "r": 160}}
            else:
                new_attr["viz"] = {"color": {"b": 255, "g": 0, "r": 0}}
            new_attr["node_type"] = "relation"
            new_attr["weight"] = min(
                self._graph.nodes[source]["weight"], self._graph.nodes[target]["weight"]
            )
            self._graph.add_node(node, **new_attr)
            self._graph.add_edge(source, node)
            self._graph.add_edge(node, target)
            self._graph.remove_edge(source, target, key=key)

    @staticmethod
    def _fix_gexf(root_element):
        graph_node = root_element.find("{http://www.gexf.net/1.1draft}graph")
        attributes_nodes = graph_node.findall(
            "{http://www.gexf.net/1.1draft}attributes"
        )
        edge_attributes = {}
        node_attributes = {}
        for attributes_node in attributes_nodes:
            for attribute_node in attributes_node.findall(
                "{http://www.gexf.net/1.1draft}attribute"
            ):
                attr_id = attribute_node.get("id")
                attr_title = attribute_node.get("title")
                attribute_node.set("id", attr_title)
                if attributes_node.get("class") == "edge":
                    edge_attributes[attr_id] = attr_title
                elif attributes_node.get("class") == "node":
                    node_attributes[attr_id] = attr_title
        nodes_node = graph_node.find("{http://www.gexf.net/1.1draft}nodes")
        for node_node in nodes_node.findall("{http://www.gexf.net/1.1draft}node"):
            attvalues_node = node_node.find("{http://www.gexf.net/1.1draft}attvalues")
            if attvalues_node is not None:
                for attvalue_node in attvalues_node.findall(
                    "{http://www.gexf.net/1.1draft}attvalue"
                ):
                    attr_for = attvalue_node.get("for")
                    attvalue_node.set("for", node_attributes[attr_for])
        edges_node = graph_node.find("{http://www.gexf.net/1.1draft}edges")
        for edge_node in edges_node.findall("{http://www.gexf.net/1.1draft}edge"):
            attvalues_node = edge_node.find("{http://www.gexf.net/1.1draft}attvalues")
            if attvalues_node is not None:
                for attvalue_node in attvalues_node.findall(
                    "{http://www.gexf.net/1.1draft}attvalue"
                ):
                    attr_for = attvalue_node.get("for")
                    if edge_attributes[attr_for] == "label":
                        attr_value = attvalue_node.get("value")
                        edge_node.set("label", attr_value)
                        attvalues_node.remove(attvalue_node)
                    attvalue_node.set("for", edge_attributes[attr_for])


class TextReltuples:
    def __init__(
        self,
        conllu,
        udpipe_model,
        w2v_model,
        stopwords,
        additional_relations,
        entities_limit,
    ):
        sentences = udpipe_model.read(conllu, "conllu")
        self._reltuples: Sequence[SentenceReltuples] = []
        self._dict = {}
        self._graph = RelGraph()
        for s in sentences:
            sentence_reltuples = SentenceReltuples(
                s,
                w2v_model,
                additional_relations=additional_relations,
                stopwords=stopwords,
            )
            self._reltuples.append(sentence_reltuples)
        cluster_labels = self._cluster(
            min_cluster_size=MIN_CLUSTER_SIZE, max_cluster_size=MIN_CLUSTER_SIZE + 50,
        )
        for sentence_reltuples, cluster in zip(self._reltuples, cluster_labels):
            self._graph.add_sentence_reltuples(sentence_reltuples, cluster=cluster)
            self._dict[sentence_reltuples.sentence.getText()] = [
                (reltuple.left_arg, reltuple.relation, reltuple.right_arg)
                for reltuple in sentence_reltuples
            ]
        self._graph.merge_relations()
        self._graph.filter_nodes(entities_limit)

    @property
    def graph(self):
        return self._graph

    @property
    def dictionary(self):
        return self._dict

    # TODO iterate over reltuples by __iter__?

    def _cluster(
        self, min_cluster_size=10, max_cluster_size=100, cluster_size_step=10
    ) -> List[int]:
        X = np.array(
            [
                sentence_reltuples.sentence_vector
                for sentence_reltuples in self._reltuples
            ]
        )
        max_sil_score = -1
        n_sentences = len(self._reltuples)
        res_labels = np.zeros(n_sentences)
        for cluster_size in range(
            min_cluster_size, max_cluster_size, cluster_size_step
        ):
            n_clusters = n_sentences // cluster_size
            if n_clusters < 2:
                continue
            clusterer = KMedoids(
                n_clusters=n_clusters, init="k-medoids++", metric="cosine"
            )
            clusterer.fit(X)
            score = silhouette_score(X, clusterer.labels_)
            if score >= max_sil_score:
                max_sil_score = score
                res_labels = clusterer.labels_
        return res_labels.tolist()


def _get_phrase_vector(sentence, words_ids, w2v_model) -> np.ndarray:
    if words_ids == "all":
        words_ids = range(len(sentence.words))
    vector = np.zeros(300)
    count = 0
    for word_id in words_ids:
        try:
            vector = np.add(
                vector,
                w2v_model[
                    "{}_{}".format(
                        sentence.words[word_id].lemma, sentence.words[word_id].upostag
                    )
                ],
            )
            count += 1
        except KeyError:
            continue
    if count > 0:
        return vector / count
    else:
        return vector


def build_dir_graph(
    conllu_dir: Path,
    save_dir: Path,
    udpipe_model: UDPipeModel,
    stopwords: List[str],
    additional_relations: bool,
    entities_limit: int,
    w2v_model,
):
    conllu = ""
    for path in tqdm(conllu_dir.glob("*.conllu")):
        with path.open("r", encoding="utf8") as conllu_file:
            conllu = "{}\n{}".format(conllu, conllu_file.read())

    text_reltuples = TextReltuples(
        conllu, udpipe_model, w2v_model, stopwords, additional_relations, entities_limit
    )

    json_path = save_dir / "relations_{}.json".format(conllu_dir.name)
    with json_path.open("w", encoding="utf8") as json_file:
        json.dump(text_reltuples.dictionary, json_file, ensure_ascii=False, indent=4)

    graph_path = save_dir / "graph_{}.gexf".format(conllu_dir.name)
    text_reltuples.graph.save(graph_path)
    print(text_reltuples.graph.nodes_number, text_reltuples.graph.edges_number)


if __name__ == "__main__":
    logging.basicConfig(
        handlers=[logging.FileHandler("logs/server.log", "a", "utf-8")],
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    parser = argparse.ArgumentParser()
    parser.add_argument("model_path", help="Path to the UDPipe model")
    parser.add_argument(
        "conllu_dir",
        help="Path to the directory containing parsed text in conllu format",
    )
    parser.add_argument("save_dir", help="Path to the directory to save relations to")
    parser.add_argument(
        "--add", help="Include additional relations", action="store_true"
    )
    parser.add_argument(
        "--entities-limit",
        help="Filter extracted relations to only contain this many entities",
        type=int,
    )
    args = parser.parse_args()
    conllu_dir = Path(args.conllu_dir)
    save_dir = Path(args.save_dir)
    udpipe_model = UDPipeModel(args.model_path)
    entities_limit = args.entities_limit or float("inf")
    with open("stopwords.txt", mode="r", encoding="utf-8") as file:
        stopwords = list(file.read().split())
    w2v_model = gensim.downloader.load("word2vec-ruscorpora-300")

    build_dir_graph(
        conllu_dir,
        save_dir,
        udpipe_model,
        stopwords,
        args.add,
        entities_limit,
        w2v_model,
    )
