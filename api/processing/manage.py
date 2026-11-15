"""Explicit operations against Temporal; no SQL job journal or scheduler."""
import argparse
import asyncio
from uuid import uuid4

from temporalio.client import WorkflowExecutionStatus
from processing.config import PIPELINE_QUEUE, pipeline_settings, temporal_client
import paper_repository as repository


async def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--reprocess-catalog', action='store_true')
    group.add_argument('--retry', metavar='WORKFLOW_ID')
    group.add_argument('--rebuild', metavar='INDEX_WORKFLOW_ID')
    args = parser.parse_args()
    client = await temporal_client()
    if args.reprocess_catalog:
        name = 'ReprocessCatalogWorkflow'
        identity = f'reprocess:{uuid4().hex}'
        payload = {'settings': pipeline_settings()}
    else:
        handle = client.get_workflow_handle(args.retry or args.rebuild)
        info = await handle.describe()
        if info.status == WorkflowExecutionStatus.RUNNING:
            raise ValueError('This workflow is still running')
        memo = await info.memo()
        if 'input' not in memo or 'workflow_type' not in memo:
            raise ValueError('Legacy workflow: submit its import again or use --reprocess-catalog')
        name = memo['workflow_type']
        payload = {key: value for key, value in memo['input'].items() if key not in ('cursor', 'prepared', 'progress', 'paused')}
        identity = f'{args.retry or args.rebuild}:retry:{uuid4().hex}'
        if args.rebuild:
            if name != 'IndexWorkflow':
                raise ValueError('--rebuild requires an IndexWorkflow')
            payload['force'] = True
    await client.start_workflow(name, payload, id=identity, task_queue=PIPELINE_QUEUE,
                                memo={'workflow_type': name, 'input': payload})
    if name == 'ImportWorkflow':
        await repository.set_import_workflow(payload['id'], identity)
    print(identity)


if __name__ == '__main__':
    asyncio.run(main())
