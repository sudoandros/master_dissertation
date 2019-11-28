import argparse
import io
import json
import string
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import matplotlib.pyplot as plt
import networkx as nx
from tqdm import tqdm

from udpipe_model import UDPipeModel


class SentenceReltuples:
    def __init__(self, sentence):
        self.reltuples = []
        self.sentence = sentence
        self._extract_reltuples()

    @property
    def string_tuples(self):
        res = []
        for reltuple in self.reltuples:
            res.append(self.to_string_tuple(reltuple))
        return res

    def to_string_tuple(self, reltuple):
        left = self.arg_to_string(reltuple[0])
        center = self.relation_to_string(reltuple[1])
        right = self.arg_to_string(reltuple[2])
        return (left, center, right)

    def _extract_reltuples(self):
        verbs = [word for word in self.sentence.words if word.upostag == "VERB"]
        for verb in verbs:
            self._extract_verb_reltuples(verb)
        copulas = [word for word in self.sentence.words if word.deprel == "cop"]
        for copula in copulas:
            self._extract_copula_reltuples(copula)

    def _extract_verb_reltuples(self, verb):
        for child_idx in verb.children:
            child = self.sentence.words[child_idx]
            if child.deprel == "xcomp":
                return
        subjects = self._get_subjects(verb)
        right_args = self._get_right_args(verb)
        for subj in subjects:
            for arg in right_args:
                relation = self._get_relation(verb, right_arg=arg)
                self.reltuples.append((subj, relation, arg))

    def _extract_copula_reltuples(self, copula):
        right_arg = self._get_right_args(copula)[0]
        parent = self.sentence.words[copula.head]
        subjects = self._get_subjects(parent)
        relation = self._get_copula(parent)
        for subj in subjects:
            self.reltuples.append((subj, relation, right_arg))

    def _get_relation(self, word, right_arg=None):
        prefix = self._get_relation_prefix(word)
        postfix = self._get_relation_postfix(word, right_arg=right_arg)
        relation = prefix + [word.id] + postfix
        return relation

    def relation_to_string(self, relation):
        return " ".join(self.sentence.words[id_].form for id_ in relation)

    def _get_relation_prefix(self, relation):
        prefix = []
        for child_idx in relation.children:
            child = self.sentence.words[child_idx]
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
        for child_idx in relation.children:
            child = self.sentence.words[child_idx]
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

    def arg_to_string(self, words_ids, lemmatized=False):
        if lemmatized:
            return " ".join(self.sentence.words[id_].lemma for id_ in words_ids)
        else:
            return " ".join(self.sentence.words[id_].form for id_ in words_ids)

    def _get_right_args(self, word):
        if word.deprel == "cop":
            args_list = self._get_copula_right_args(word)
        else:
            args_list = self._get_verb_right_args(word)
        return args_list

    def _get_copula_right_args(self, word):
        parent = self.sentence.words[word.head]
        words_ids = self._get_subtree(parent)
        copula_words_ids = self._get_copula(parent)
        for id_ in copula_words_ids:
            words_ids.remove(id_)
        # remove subject subtree
        for id_ in words_ids.copy():
            word_to_check = self.sentence.words[id_]
            if word_to_check.head == word.id and self._is_subject(word_to_check):
                subtree = self._get_subtree(word_to_check)
                for id_to_remove in subtree:
                    words_ids.remove(id_to_remove)
        return [words_ids]

    def _get_subtree(self, word):
        if not list(word.children):
            return [word.id]
        res_ids = []
        for child_idx in (idx for idx in word.children if idx < word.id):
            child = self.sentence.words[child_idx]
            res_ids.extend(self._get_subtree(child))
        res_ids.append(word.id)
        for child_idx in (idx for idx in word.children if idx > word.id):
            child = self.sentence.words[child_idx]
            res_ids.extend(self._get_subtree(child))
        return res_ids

    def _get_subjects(self, word):
        subj_list = []
        for child_idx in word.children:
            child = self.sentence.words[child_idx]
            if self._is_subject(child):
                subj_list.append(self._get_subtree(child))
        if not subj_list and (word.deprel == "conj" or word.deprel == "xcomp"):
            parent = self.sentence.words[word.head]
            subj_list = self._get_subjects(parent)
        return subj_list

    def _get_verb_right_args(self, word):
        args_list = []
        for child_idx in word.children:
            child = self.sentence.words[child_idx]
            if self._is_right_arg(child):
                args_list.append(self._get_subtree(child))
        parent = self.sentence.words[word.head]
        if word.deprel == "xcomp":
            args_list += self._get_verb_right_args(parent)
        if self._is_conjunct(word) and parent.deprel == "xcomp":
            grandparent = self.sentence.words[parent.head]
            args_list += self._get_verb_right_args(grandparent)
        return args_list

    def _get_first_case(self, words_ids):
        for id_ in words_ids:
            word = self.sentence.words[id_]
            if word.head not in words_ids:
                root = word
        for id_ in words_ids:
            word = self.sentence.words[id_]
            if id_ < root.id and word.deprel == "case":
                return id_
        return None

    def _get_copula(self, word):
        part_ids = []
        for child_idx in word.children:
            child = self.sentence.words[child_idx]
            if child.deprel == "cop":
                return part_ids + [child.id]
            if child.upostag == "PART":
                part_ids.append(child.id)
            else:
                part_ids = []
        return []

    def _is_subject(self, word):
        return word.deprel in ["nsubj", "nsubj:pass"]

    def _is_right_arg(self, word):
        return word.deprel in ["obj", "iobj", "obl", "obl:agent", "iobl"]

    def _is_conjunct(self, word):
        return word.deprel == "conj"


class RelGraph:
    def __init__(self, stopwords):
        self._graph = nx.DiGraph()
        self._stopwords = set(stopwords)

    @classmethod
    def from_reltuples_list(cls, stopwords, reltuples_list):
        graph = cls(stopwords)
        for sentence_reltuple in reltuples_list:
            graph.add_sentence_reltuples(sentence_reltuple)

    def add_sentence_reltuples(self, sentence_reltuples, include_syntax=False):
        for reltuple in sentence_reltuples.reltuples:
            self._add_reltuple(
                reltuple, sentence_reltuples, include_syntax=include_syntax
            )

    def _add_reltuple(self, reltuple, sentence_reltuples, include_syntax=False):
        source_name = sentence_reltuples.arg_to_string(reltuple[0], lemmatized=True)
        source_label = sentence_reltuples.arg_to_string(reltuple[0])
        target_name = sentence_reltuples.arg_to_string(reltuple[2], lemmatized=True)
        target_label = sentence_reltuples.arg_to_string(reltuple[2])
        relation = sentence_reltuples.relation_to_string(reltuple[1])
        sentence_text = sentence_reltuples.sentence.getText()
        self._add_node(source_name, sentence_text, label=source_label)
        self._add_node(target_name, sentence_text, label=target_label)
        self._add_edge(source_name, target_name, relation, sentence_text)
        if include_syntax:
            self._add_syntax_tree(reltuple[0], sentence_reltuples)
            self._add_syntax_tree(reltuple[2], sentence_reltuples)

    def _add_edge(self, source, target, label, description):
        source = self._clean_string(source)
        target = self._clean_string(target)
        label = self._clean_string(label)
        if source not in self._graph or target not in self._graph:
            return
        if not self._graph.has_edge(source, target):
            self._graph.add_edge(
                source, target, label=label, description=description, weight=1
            )
            return
        # this edge already exists
        if label not in self._graph[source][target]["label"].split(" | "):
            self._graph[source][target]["label"] = "{} | {}".format(
                self._graph[source][target]["label"], label
            )
        if description not in self._graph[source][target]["description"].split(" | "):
            self._graph[source][target]["description"] = "{} | {}".format(
                self._graph[source][target]["description"], description
            )
        self._graph[source][target]["weight"] += 1

    def _add_node(self, name, description, label=None):
        name = self._clean_string(name)
        if label:
            label = self._clean_string(label)
        else:
            label = name
        if set(name.split()).issubset(self._stopwords) or (
            len(name) == 1 and name.isalpha()
        ):
            return
        if name not in self._graph:
            self._graph.add_node(name, label=label, description=description, weight=1)
            return
        # this node already exists
        if description not in self._graph.nodes[name]["description"].split(" | "):
            self._graph.nodes[name]["description"] = "{} | {}".format(
                self._graph.nodes[name]["description"], description
            )
        self._graph.nodes[name]["weight"] += 1

    def save(self, path):
        stream_buffer = io.BytesIO()
        nx.write_gexf(self._graph, stream_buffer, encoding="utf-8", version="1.1draft")
        xml_string = stream_buffer.getvalue().decode("utf-8")
        root_element = ET.fromstring(xml_string)
        self._fix_gexf(root_element)
        ET.register_namespace("", "http://www.gexf.net/1.1draft")
        xml_tree = ET.ElementTree(root_element)
        xml_tree.write(path, encoding="utf-8")

    def _add_syntax_tree(self, rel_arg, sentence_reltuples):
        full_arg_string = sentence_reltuples.arg_to_string(rel_arg)
        self._add_word(rel_arg, sentence_reltuples)
        self._add_edge(
            rel_arg.lemma,
            full_arg_string,
            rel_arg.deprel,
            sentence_reltuples.sentence.getText(),
        )

    def _add_word(self, word, sentence_reltuples):
        self._add_node(word.lemma, sentence_reltuples.sentence.getText())
        parent = sentence_reltuples.sentence.words[word.head]
        self._add_edge(
            word.lemma, parent.lemma, word.deprel, sentence_reltuples.sentence.getText()
        )
        for child_idx in word.children:
            child = sentence_reltuples.sentence.words[child_idx]
            self._add_word(child, sentence_reltuples)

    def _clean_string(self, node_string):
        res = (
            "".join(
                char
                for char in node_string
                if char.isalnum() or char.isspace() or char in ",.;-—/:%"
            )
            .lower()
            .strip(" .,:;-")
        )
        return res

    def _fix_gexf(self, root_element):
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("model_path", help="Path to the UDPipe model")
    parser.add_argument(
        "conllu_dir",
        help="Path to the directory containing parsed text in conllu format",
    )
    parser.add_argument("save_dir", help="Path to the directory to save relations to")
    parser.add_argument(
        "--include-syntax",
        help="Include syntax tree of every phrase in the graph",
        action="store_true",
    )
    args = parser.parse_args()
    conllu_dir = Path(args.conllu_dir)
    save_dir = Path(args.save_dir)
    model = UDPipeModel(args.model_path)
    with open("stopwords.txt", mode="r", encoding="utf-8") as file:
        stopwords = list(file.read().split())

    graph = RelGraph(stopwords)
    for path in tqdm(conllu_dir.iterdir()):
        output = {}
        if not (path.suffix == ".conllu"):
            continue
        with path.open("r", encoding="utf8") as file:
            text = file.read()
        sentences = model.read(text, "conllu")
        for s in sentences:
            reltuples = SentenceReltuples(s)
            output[s.getText()] = reltuples.string_tuples
            graph.add_sentence_reltuples(reltuples, include_syntax=args.include_syntax)

        output_path = save_dir / (path.stem + "_reltuples.json")
        with output_path.open("w", encoding="utf8") as file:
            json.dump(output, file, ensure_ascii=False, indent=4)

    graph.save(save_dir / "graph.gexf")

