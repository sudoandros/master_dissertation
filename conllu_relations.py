import sys
import json
from tqdm import tqdm
from pathlib import Path
import argparse
from udpipe_model import UDPipeModel

UDPIPE_MODEL_PATH = "data/udpipe_models/russian-syntagrus-ud-2.3-181115.udpipe"


class SentenceRelations:
    def __init__(self, sentence):
        self.relations = []
        self.sentence = sentence
        self._extract_relations()

    def _extract_relations(self):
        self._extract_verb_relations()

    def _extract_verb_relations(self):
        verbs = [word for word in self.sentence.words if word.upostag == "VERB"]
        all_subjects = [self._get_subjects(verb) for verb in verbs]
        all_objects = [self._get_objects(verb) for verb in verbs]
        all_oblique_nominals = [self._get_oblique_nominals(verb) for verb in verbs]
        for i, verb in enumerate(verbs):
            print(verb.form)
            verb_subjects = all_subjects[i]
            verb_objects = all_objects[i]
            verb_oblique_nominals = all_oblique_nominals[i]
            if not verb_subjects:
                try:
                    verb_subjects = all_subjects[i - 1]
                    all_subjects[i] = verb_subjects
                except IndexError:
                    pass
            for subj in verb_subjects:
                for obj in verb_objects:
                    self.relations.append((subj, verb, obj))
            for subj in verb_subjects:
                for obl in verb_oblique_nominals:
                    self.relations.append((subj, verb, obl))

    def _get_subjects(self, word):
        subj_list = []
        for child_idx in word.children:
            child = self.sentence.words[child_idx]
            if child.deprel in ["nsubj", "nsubj:pass"]:
                subj_list.append(child)
                subj_list += self._get_conjuncts(child)
        return subj_list

    def _get_objects(self, word):
        obj_list = []
        for child_idx in word.children:
            child = self.sentence.words[child_idx]
            if child.deprel in ["obj", "iobj"]:
                obj_list.append(child)
                obj_list += self._get_conjuncts(child)
        return obj_list

    def _get_oblique_nominals(self, word):
        obl_list = []
        for child_idx in word.children:
            child = self.sentence.words[child_idx]
            if child.deprel in ["obl", "obl:agent"]:
                obl_list.append(child)
                obl_list += self._get_conjuncts(child)
        return obl_list

    def _get_conjuncts(self, word):
        conjuncts = []
        for child_idx in word.children:
            child = self.sentence.words[child_idx]
            if child.deprel == "conj":
                conjuncts.append(child)
        return conjuncts


def get_case(sentence, word):
    for ichild in word.children:
        child = sentence.words[ichild]
        if child.deprel == "case":
            return child.form


def get_punct(sentence, word):
    for ichild in word.children:
        child = sentence.words[ichild]
        if child.deprel == "punct":
            return child.form


def get_conj(sentence, word):
    for ichild in word.children:
        child = sentence.words[ichild]
        if child.deprel == "cc":
            return child.form


def get_obj(sentence, verb):
    res = []
    for ichild in verb.children:
        child = sentence.words[ichild]
        if child.deprel in ["obj", "iobj"]:
            res.append(child.form)
    return res


def get_obl(sentence, verb):
    res = []
    for ichild in verb.children:
        child = sentence.words[ichild]
        if child.deprel in ["obl", "obl:agent"]:
            case = get_case(sentence, child)
            if case:
                res.append(get_case(sentence, child) + " " + child.form)
            else:
                res.append(child.form)
    return res


def get_subj(sentence, verb):
    res = []
    for ichild in verb.children:
        child = sentence.words[ichild]
        if child.deprel in ["nsubj", "nsubj:pass"]:
            res.append(child.form)
            for igrandchild in child.children:
                grandchild = sentence.words[igrandchild]
                if grandchild.deprel == "conj":
                    punct = get_punct(sentence, grandchild)
                    conj = get_conj(sentence, grandchild)
                    if punct:
                        res[-1] += punct + " " + grandchild.form
                    elif conj:
                        res[-1] += " " + conj + " " + grandchild.form
    return res


def get_aux(sentence, verb):
    for ichild in verb.children:
        child = sentence.words[ichild]
        if child.deprel == "aux":
            return child.form


def verb_rel(sentence):
    res = []
    for word in sentence.words:
        if word.upostag == "VERB":
            verb = word.form
            aux = get_aux(sentence, word)
            if aux:
                verb = aux + " " + verb

            subj_list = get_subj(sentence, word)
            obj_list = get_obj(sentence, word)
            obl_list = get_obl(sentence, word)

            for subj in subj_list:
                for obj in obj_list:
                    res.append([subj, verb, obj])
            for subj in subj_list:
                for obl in obl_list:
                    res.append([subj, verb, obl])

            for ichild in word.children:
                child = sentence.words[ichild]
                if child.deprel == "conj":
                    verb = child.form
                    aux = get_aux(sentence, child)
                    if aux:
                        verb = aux + " " + verb

                    subj_list = get_subj(sentence, child) or subj_list
                    obj_list = get_obj(sentence, child)
                    obl_list = get_obl(sentence, child)

                    for subj in subj_list:
                        for obj in obj_list:
                            res.append([subj, verb, obj])
                    for subj in subj_list:
                        for obl in obl_list:
                            res.append([subj, verb, obl])
    return res


def get_relations(sentence):
    res = []
    res += verb_rel(sentence)
    return res


def simple_test(model):
    relations = []
    sentences = model.tokenize(
        "Андрей пошел в магазин и аптеку, купил куртку и телефон, на улице становилось темно. Никита бегал в парке, а Андрей, Дима и Федор варили и жарили обед. Андрей, пока собирался на работу, съел завтрак."
    )
    for s in sentences:
        model.tag(s)
        model.parse(s)
        relations = SentenceRelations(s)

    conllu = model.write(sentences, "conllu")
    with open("output.conllu", "w", encoding="utf8") as f:
        f.write(conllu)


# TODO частицы
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("syntax_parser", choices=["syntaxnet", "udpipe"])
    parser.add_argument(
        "directory", help="Path to directory containing parsed text in conllu format"
    )
    args = parser.parse_args()
    dir_path = Path(args.directory)
    model = UDPipeModel(UDPIPE_MODEL_PATH)

    simple_test(model)

    # for path in tqdm(dir_path.iterdir()):
    #     relations = {}
    #     text = None
    #     if not (
    #         (args.syntax_parser == "syntaxnet" and "_syntaxnet.conllu" in path.name)
    #         or (args.syntax_parser == "udpipe" and "_udpiped.conllu" in path.name)
    #     ):
    #         continue

    #     with path.open("r", encoding="utf8") as f:
    #         text = f.read()

    #     sentences = model.read(text, "conllu")
    #     for s in sentences:
    #         relations[s.getText()] = get_relations(s)

    #     towrite = dir_path / (path.stem + "_relations.json")
    #     with towrite.open("w", encoding="utf8") as f:
    #         json.dump(relations, f, ensure_ascii=False, indent=4)
