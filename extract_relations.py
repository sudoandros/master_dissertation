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
        left = self.left_arg_to_string(reltuple[0])
        center = self.relation_to_string(reltuple[1], right_arg=reltuple[2])
        right = self.right_arg_to_string(reltuple[2])
        return (left, center, right)

    def _extract_reltuples(self):
        verbs = [word for word in self.sentence.words if word.upostag == "VERB"]
        for verb in verbs:
            self._extract_verb_reltuples(verb)

    def _extract_verb_reltuples(self, verb):
        for child_idx in verb.children:
            child = self.sentence.words[child_idx]
            if child.deprel == "xcomp":
                return
        verb_subjects = self._get_subjects(verb)
        verb_objects = self._get_objects(verb)
        verb_oblique_nominals = self._get_oblique_nominals(verb)
        for subj in verb_subjects:
            for obj in verb_objects:
                self.reltuples.append((subj, verb, obj))
        for subj in verb_subjects:
            for obl in verb_oblique_nominals:
                self.reltuples.append((subj, verb, obl))

    def relation_to_string(self, relation, right_arg=None):
        prefix = self._get_relation_prefix(relation)
        postfix = self._get_relation_postfix(relation, right_arg)
        return prefix + relation.form + postfix

    def _get_relation_prefix(self, relation):
        prefix = ""
        for child_idx in relation.children:
            child = self.sentence.words[child_idx]
            if (
                child.deprel == "case"
                or child.deprel == "aux"
                or child.deprel == "aux:pass"
                or child.upostag == "PART"
            ) and child.id < relation.id:
                prefix += child.form + " "
        parent = self.sentence.words[relation.head]
        if relation.deprel == "xcomp":
            prefix = self.relation_to_string(parent) + " " + prefix
        if self._is_conjunct(relation) and parent.deprel == "xcomp":
            grandparent = self.sentence.words[parent.head]
            prefix = self.relation_to_string(grandparent) + " " + prefix
        return prefix

    def _get_relation_postfix(self, relation, right_arg=None):
        postfix = ""
        for child_idx in relation.children:
            child = self.sentence.words[child_idx]
            if (
                child.deprel == "case"
                or child.deprel == "aux"
                or child.deprel == "aux:pass"
                or child.upostag == "PART"
            ) and child.id > relation.id:
                postfix += " " + child.form
        if right_arg:
        case = self._get_first_case(right_arg)
        if case is not None:
            postfix += " " + case.form
        return postfix

    def left_arg_to_string(self, word, lemmatized=False):
        words_ids = self._get_arg_ids(word)
        if lemmatized:
            return " ".join(self.sentence.words[id_].lemma for id_ in words_ids)
        else:
            return " ".join(self.sentence.words[id_].form for id_ in words_ids)

    def right_arg_to_string(self, word, lemmatized=False):
        first_case = self._get_first_case(word)
        words_ids = self._get_arg_ids(word, exclude=first_case)
        if lemmatized:
            return " ".join(self.sentence.words[id_].lemma for id_ in words_ids)
        else:
            return " ".join(self.sentence.words[id_].form for id_ in words_ids)

    def _get_arg_ids(self, word, exclude=None, lemmatized=False):
        if not list(word.children):
            return [word.id]
        res_ids = []
        for child_idx in (idx for idx in word.children if idx < word.id):
            child = self.sentence.words[child_idx]
            if exclude and exclude.id == child.id:
                continue
            res_ids.extend(self._get_arg_ids(child, exclude=exclude))
        res_ids.append(word.id)
        for child_idx in (idx for idx in word.children if idx > word.id):
            child = self.sentence.words[child_idx]
            res_ids.extend(self._get_arg_ids(child, exclude=exclude))
        return res_ids

    def _get_subjects(self, word):
        subj_list = []
        for child_idx in word.children:
            child = self.sentence.words[child_idx]
            if self._is_subject(child):
                subj_list.append(child)
        if not subj_list and (word.deprel == "conj" or word.deprel == "xcomp"):
            parent = self.sentence.words[word.head]
            subj_list = self._get_subjects(parent)
        return subj_list

    def _get_objects(self, word):
        obj_list = []
        for child_idx in word.children:
            child = self.sentence.words[child_idx]
            if self._is_object(child):
                obj_list.append(child)
        parent = self.sentence.words[word.head]
        if word.deprel == "xcomp":
            obj_list += self._get_objects(parent)
        if self._is_conjunct(word) and parent.deprel == "xcomp":
            grandparent = self.sentence.words[parent.head]
            obj_list += self._get_objects(grandparent)
        return obj_list

    def _get_oblique_nominals(self, word):
        obl_list = []
        for child_idx in word.children:
            child = self.sentence.words[child_idx]
            if self._is_oblique_nominal(child):
                obl_list.append(child)
        parent = self.sentence.words[word.head]
        if word.deprel == "xcomp":
            obl_list += self._get_oblique_nominals(parent)
        if self._is_conjunct(word) and parent.deprel == "xcomp":
            grandparent = self.sentence.words[parent.head]
            obl_list += self._get_oblique_nominals(grandparent)
        return obl_list

    def _get_first_case(self, word):
        if len(word.children) == 0:  # no children
            return None
            child_idx = word.children[0]
        if child_idx > word.id:  # there are children only after the word
            return None
            child = self.sentence.words[child_idx]
            if child.deprel == "case":
                return child
            else:
                return self._get_first_case(child)

    def _is_subject(self, word):
        return word.deprel in ["nsubj", "nsubj:pass"]

    def _is_object(self, word):
        return word.deprel in ["obj", "iobj"]

    def _is_oblique_nominal(self, word):
        return word.deprel in ["obl", "obl:agent", "iobl"]

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
        source = sentence_reltuples.left_arg_to_string(reltuple[0])
        target = sentence_reltuples.right_arg_to_string(reltuple[2])
        relation = sentence_reltuples.relation_to_string(reltuple[1], reltuple[2])
        sentence_text = sentence_reltuples.sentence.getText()
        self._add_node(source, sentence_text)
        self._add_node(target, sentence_text)
        self._add_edge(source, target, relation, sentence_text)
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

    def _add_node(self, name, description):
        name = self._clean_string(name)
        if set(name.split()).issubset(self._stopwords) or (
            len(name) == 1 and name.isalpha()
        ):
            return
        if name not in self._graph:
            self._graph.add_node(name, description=description, weight=1)
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

