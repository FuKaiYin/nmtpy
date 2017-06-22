import subprocess

from ..sysutils import find_executable
from .bleu      import BLEUScore

"""Factors2word class."""
class Factors2word(object):
    def __init__(self):
        pass

    def compute(self, script, hyp_file, hyp_mult_file, ref):
        script = find_executable(script)
        lang = ref.split('.')[-1]
        cmdline = [script, lang, hyp_file, hyp_mult_file, ref]

        hypstring = None
        with open(hyp_file, "r") as fhyp:
            hypstring = fhyp.read().rstrip()
        
        out = subprocess.run(cmdline, stdout=subprocess.PIPE,
                               input=hypstring, universal_newlines=True).stdout.splitlines()
        # TODO: this -1 gives many problems, in future we will return just BLEU and avoid those problems
        score = out[-1].splitlines()
        if len(score) == 0:
            return BLEUScore()
        else:
            return BLEUScore(score[0].rstrip("\n"))
