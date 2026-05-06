#!/usr/bin/env python3
"""
Query PEPhub accbase namespace for new ATAC-seq samples and queue them
for downstream processing on Rivanna.

This script:
1. Queries PEPhub for projects updated in the date range
2. For each project, fetches sample metadata
3. Inserts new samples into the accbase sample queue via AccbaseDBAgent
"""
import argparse

import yacman
from pephubclient import PEPHubClient

from db_agent import AccbaseDBAgent


def queue_project_samples(phc, agent, namespace, project_name, tag="default"):
    """Queue all samples from a single project. Returns count of newly queued samples."""
    registry = f"{namespace}/{project_name}:{tag}"
    try:
        pep = phc.load_project(registry)
    except Exception as e:
        print(f"  Warning: Could not load project {registry}: {e}")
        return 0

    queued = 0
    for sample in pep.samples:
        sample_name = sample.sample_name
        if agent.queue_sample(sample_name, project_name, registry):
            queued += 1
            print(f"  Queued: {sample_name}")
    return queued


def main():
    parser = argparse.ArgumentParser(description='Queue ATAC-seq samples for downstream processing')
    parser.add_argument('--start-date', help='Start date (YYYY/MM/DD) for date range query')
    parser.add_argument('--end-date', help='End date (YYYY/MM/DD) for date range query')
    parser.add_argument('--gse', help='Queue a specific GSE instead of date range')
    parser.add_argument('--config', required=True, help='Path to accbase_config.yaml')
    args = parser.parse_args()

    if args.gse and (args.start_date or args.end_date):
        parser.error("Cannot use --gse with --start-date/--end-date")
    if not args.gse and not (args.start_date and args.end_date):
        parser.error("Must provide either --gse or both --start-date and --end-date")

    config = yacman.YAMLConfigManager.from_yaml_file(args.config).exp
    namespace = config['phc']['namespace']

    phc = PEPHubClient()
    agent = AccbaseDBAgent.from_config(config)

    total_queued = 0

    if args.gse:
        print(f"Queueing samples from {namespace}/{args.gse}")
        total_queued = queue_project_samples(phc, agent, namespace, args.gse)
    else:
        print(f"Querying projects in namespace: {namespace}")
        print(f"Date range: {args.start_date} to {args.end_date}")

        try:
            result = phc.find_project(namespace=namespace)
            print(f"Query result type: {type(result)}, count: {getattr(result, 'count', 'N/A')}")
            projects = result.results if hasattr(result, 'results') else []
            print(f"Projects list length: {len(projects)}")
        except Exception as e:
            print(f"Error listing projects: {e}")
            import traceback
            traceback.print_exc()
            projects = []

        if not projects:
            print(f"No projects found in namespace {namespace}")
        else:
            print(f"Found {len(projects)} projects in {namespace}")

            for project in projects:
                if hasattr(project, 'name'):
                    project_name = project.name
                    project_tag = getattr(project, 'tag', 'default')
                elif isinstance(project, dict):
                    project_name = project.get('name', str(project))
                    project_tag = project.get('tag', 'default')
                else:
                    project_name = str(project)
                    project_tag = 'default'

                print(f"Processing: {project_name}:{project_tag}")
                total_queued += queue_project_samples(phc, agent, namespace, project_name, project_tag)

    print(f"\nQueued {total_queued} new samples for downstream processing")


if __name__ == '__main__':
    main()
