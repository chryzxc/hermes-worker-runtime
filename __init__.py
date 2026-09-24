"""Hermes native-plugin entrypoint for Worker Runtime."""

if __package__:
    from .hermes_worker_runtime import register
else:  # pytest may import the repository root without a package name.
    from hermes_worker_runtime import register

__all__ = ["register"]
