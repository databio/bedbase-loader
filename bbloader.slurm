#!/bin/bash

#SBATCH --begin=15:30  #Starts at 15:00
#SBATCH --account=shefflab
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=20000
#SBATCH --partition=standard
#SBATCH --time=3:00:00
#SBATCH -o /home/bnt4me/bedbase/geo/rerun/unprocessed_run.out

echo "Hello $USER, this is node $(hostname)."


sbatch /home/bnt4me/repos/bedbase-loader/bbloader.slurm  # Resubmits itself

echo "Running processing of bed files that have not been processed yet."


source /home/bnt4me/venv/refgenie/bin/activate
source /home/bnt4me/v_env/.env

go


bedboss reprocess-all --bedbase-config /home/bnt4me/bedbase/api.bedbase.org.yaml --outfolder /project/shefflab/brickyard/results_pipeline/bedbase/geo --limit 5
