"""SQLite persistence shared by the Collector and the API (ADR-0002).

A leaf layer: it may use ``domain`` but never ``collector``, ``api`` or an exchange library. The migration
runner lives in ``storage.migrate`` and is imported only by the Collector.
"""
