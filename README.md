# BEDbase loader

This repository contains GitHub Actions workflows for automated BED file uploads to BEDbase.

For complete documentation on the data loading architecture, see: [BEDbase Data Loading](https://docs.bedbase.org/bedbase/bedbase-data-loading/)

## Accbase Loading

Accbase workflows queue ATAC-seq samples for processing by Forge on Rivanna HPC.

| Workflow | Schedule | Purpose |
|----------|----------|---------|
| `accbase_queue_cron.yml` | Daily 19:00 UTC | Queue new samples from accbase namespace |
| `accbase_queue_gse.yml` | Manual | Queue samples from a specific GSE |

Unlike BEDbase (which processes files in GitHub Actions + Fargate), Accbase only
queues samples in the forge_queue table. Heavy processing (PEPATAC pipeline) runs
on Rivanna HPC via the Forge system.

Configuration: `accbase_config.yaml`

### Required Secrets

For Accbase workflows:
- `ACCBASE_POSTGRES_HOST` - PostgreSQL host for accbase
- `ACCBASE_POSTGRES_USER` - Database user
- `ACCBASE_POSTGRES_PASSWORD` - Database password
- `PEPHUB_API_KEY` - Optional, for higher rate limits

### Forge Queue Consumer

The forge_queue table is consumed by Forge on Rivanna:

1. Forge queries for samples with status='pending_forge'
2. Generates looper submission from PEPhub PEP
3. Submits SLURM jobs via looper
4. Updates status to 'processing'
5. On completion, updates status to 'completed'

This decouples data discovery (GitHub Actions) from heavy processing (Rivanna HPC).
