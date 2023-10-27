#!/usr/bin/env python3

import sys
import subprocess

EXP_DIR = "exps/DETReg_fine_tune_full_pascal"
PY_ARGS = sys.argv[1:]

command = [
    "./configs/DETReg_fine_tune_full_pascal.sh",
    "--resume", "exps/DETReg_fine_tune_full_pascal/checkpoint0099.pth",
    "--viz"
] + PY_ARGS

subprocess.run(command)
