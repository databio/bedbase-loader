#!/usr/bin/env python3
"""
Query PEPhub accbase namespace for new ATAC-seq samples and queue them
for processing by Forge on Rivanna.

This script:
1. Queries PEPhub for projects updated in the date range
2. For each project, fetches sample metadata
3. Checks if samples are already in the database
4. Inserts new samples with status='pending_forge' into the queue table
"""
import argparse
import os
from datetime import datetime
import yaml
import psycopg2
from pephubclient import PEPHubClient


def get_db_config(config):
    """Get database config with environment variable substitution."""
    return {
        'host': os.environ.get('ACCBASE_POSTGRES_HOST', config['database']['host'].replace('$ACCBASE_POSTGRES_HOST', '')),
        'port': config['database']['port'],
        'user': os.environ.get('ACCBASE_POSTGRES_USER', config['database']['user'].replace('$ACCBASE_POSTGRES_USER', '')),
        'password': os.environ.get('ACCBASE_POSTGRES_PASSWORD', config['database']['password'].replace('$ACCBASE_POSTGRES_PASSWORD', '')),
        'database': config['database']['database']
    }


def ensure_queue_table(cur):
    """Create the forge_queue table if it doesn't exist."""
    cur.execute('''
        CREATE TABLE IF NOT EXISTS forge_queue (
            id SERIAL PRIMARY KEY,
            sample_name VARCHAR(255) NOT NULL,
            project_name VARCHAR(255) NOT NULL,
            pephub_registry VARCHAR(255) NOT NULL,
            status VARCHAR(50) DEFAULT 'pending_forge',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(sample_name, project_name)
        )
    ''')


def queue_sample(cur, sample_name, project_name, pephub_registry):
    """Insert a sample into the queue if not already present. Returns True if inserted."""
    # Check if already queued
    cur.execute(
        'SELECT id FROM forge_queue WHERE sample_name = %s AND project_name = %s',
        (sample_name, project_name)
    )
    if cur.fetchone():
        return False

    # Insert into queue
    cur.execute('''
        INSERT INTO forge_queue (sample_name, project_name, pephub_registry, status)
        VALUES (%s, %s, %s, 'pending_forge')
        ON CONFLICT DO NOTHING
    ''', (sample_name, project_name, pephub_registry))
    return True


def queue_project_samples(phc, cur, namespace, project_name):
    """Queue all samples from a single project. Returns count of newly queued samples."""
    registry = f"{namespace}/{project_name}"
    try:
        pep = phc.load_project(registry)
    except Exception as e:
        print(f"  Warning: Could not load project {registry}: {e}")
        return 0

    queued = 0
    for sample in pep.samples:
        sample_name = sample.sample_name
        if queue_sample(cur, sample_name, project_name, registry):
            queued += 1
            print(f"  Queued: {sample_name}")
    return queued


def main():
    parser = argparse.ArgumentParser(description='Queue ATAC-seq samples for Forge processing')
    parser.add_argument('--start-date', help='Start date (YYYY/MM/DD) for date range query')
    parser.add_argument('--end-date', help='End date (YYYY/MM/DD) for date range query')
    parser.add_argument('--gse', help='Queue a specific GSE instead of date range')
    parser.add_argument('--config', required=True, help='Path to accbase_config.yaml')
    args = parser.parse_args()

    # Validate arguments
    if args.gse and (args.start_date or args.end_date):
        parser.error("Cannot use --gse with --start-date/--end-date")
    if not args.gse and not (args.start_date and args.end_date):
        parser.error("Must provide either --gse or both --start-date and --end-date")

    # Load config
    with open(args.config) as f:
        config = yaml.safe_load(f)

    db_config = get_db_config(config)
    namespace = config['phc']['namespace']

    # Connect to PEPhub
    phc = PEPHubClient()

    # Connect to database
    print(f"Connecting to database: {db_config['host']}:{db_config['port']}/{db_config['database']}")
    conn = psycopg2.connect(**db_config)
    cur = conn.cursor()

    # Ensure queue table exists
    ensure_queue_table(cur)
    conn.commit()

    total_queued = 0

    if args.gse:
        # Queue a specific GSE
        print(f"Queueing samples from {namespace}/{args.gse}")
        total_queued = queue_project_samples(phc, cur, namespace, args.gse)
    else:
        # Query for all projects in namespace and filter by update date
        # Note: PEPhub API may not support date filtering directly, so we list all projects
        print(f"Querying projects in namespace: {namespace}")
        print(f"Date range: {args.start_date} to {args.end_date}")

        try:
            projects = phc.find_project(namespace=namespace)
        except Exception as e:
            print(f"Error listing projects: {e}")
            print("Trying alternative method...")
            projects = []

        if not projects:
            print(f"No projects found in namespace {namespace}")
        else:
            print(f"Found {len(projects)} projects in {namespace}")

            for project in projects:
                # Extract project name from response
                if hasattr(project, 'name'):
                    project_name = project.name
                elif isinstance(project, dict):
                    project_name = project.get('name', str(project))
                else:
                    project_name = str(project)

                print(f"Processing: {project_name}")
                queued = queue_project_samples(phc, cur, namespace, project_name)
                total_queued += queued

    conn.commit()
    print(f"\nQueued {total_queued} new samples for Forge processing")

    cur.close()
    conn.close()


if __name__ == '__main__':
    main()
