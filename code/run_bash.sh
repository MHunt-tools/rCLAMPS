#!/bin/bash -l

# Loop to submit 100 jobs
for i in {0..249}; do
# for i in 2 3 4 7; do
    # Submit the job using sbatch
    sbatch submit_slurm.sh $i
done