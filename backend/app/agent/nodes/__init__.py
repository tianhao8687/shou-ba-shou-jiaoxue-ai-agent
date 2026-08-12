"""Workflow node implementations.

Each module exposes one ``run(engine, run, lease) -> NodeOutcome`` function.  The
node may update its business payload, but only the lifecycle/state-machine layer
may change ``current_node``.
"""
