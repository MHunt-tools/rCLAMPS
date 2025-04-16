#!/bin/bash -l

#SBATCH --job-name=MGY
#SBATCH --time=24:00:00
#SBATCH --mem=10G
#SBATCH --partition=commons

conda activate meme

python gibbsAlign_GLM.py -n $1