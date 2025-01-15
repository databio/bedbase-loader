# Description: This script checks if the bb-loader job is running and starts it if it is not.

# Find out of the mobot job is running
bbloader_job=$(squeue -n "bbloader.slurm" -u `getent group shefflab | cut -f4 -d':'` | wc -l)  # check all shefflab members

# If the mobot job is not running, start it
if [ $bbloader_job -eq 1 ]; then
  echo "No bbloader job in queue. Starting one." 1>&2
  # TODO: point to shared path
  sbatch /home/bnt4me/repos/bedbase-loader/bbloader.slurm
else if [ $bbloader_job -eq 2 ]; then
  echo "bbloader job found in queue." 1>&2
else
  echo "More than one bbloader job found in queue. Exiting." 1>&2
fi
fi