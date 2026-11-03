"""SQL identifier parsing and service-qualified OpenMetadata identities."""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config


def _require_identifier(value: str) -> None:
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(
            "identifiers must be nonempty strings without control characters"
        )


def fqn(*parts: str) -> str:
    """Join semantic identifier parts, escaping OpenMetadata's quote delimiter."""
    if not parts:
        raise ValueError("an FQN requires at least one identifier")
    quoted = []
    for part in parts:
        _require_identifier(part)
        quoted.append(
            '"' + part.replace('"', '""') + '"' if "." in part or '"' in part else part
        )
    return ".".join(quoted)


@dataclass(frozen=True)
class TableName:
    database: str
    schema: str
    table: str

    def __post_init__(self) -> None:
        for part in (self.database, self.schema, self.table):
            _require_identifier(part)

    def fqn(self, config: Config) -> str:
        return fqn(
            config.service_for(self.database), self.database, self.schema, self.table
        )

    def database_fqn(self, config: Config) -> str:
        return fqn(config.service_for(self.database), self.database)

    def schema_fqn(self, config: Config) -> str:
        return fqn(config.service_for(self.database), self.database, self.schema)

    @property
    def sql(self) -> str:
        return ".".join(
            '"' + part.replace('"', '""') + '"'
            for part in (self.database, self.schema, self.table)
        )


def _split_sql_name(name: str) -> list[str]:
    _require_identifier(name)
    parts = []
    index = 0
    while index < len(name):
        while index < len(name) and name[index].isspace():
            index += 1
        if index == len(name):
            raise ValueError("SQL name has an empty identifier")
        if name[index] in {'"', "`", "["}:
            closing = "]" if name[index] == "[" else name[index]
            index += 1
            characters = []
            while index < len(name):
                if name[index] == closing:
                    if index + 1 < len(name) and name[index + 1] == closing:
                        characters.append(closing)
                        index += 2
                    else:
                        index += 1
                        break
                else:
                    characters.append(name[index])
                    index += 1
            else:
                raise ValueError("SQL name contains an unterminated quoted identifier")
            part = "".join(characters)
            while index < len(name) and name[index].isspace():
                index += 1
            if index < len(name) and name[index] != ".":
                raise ValueError("quoted SQL identifiers must be separated by dots")
        else:
            start = index
            while index < len(name) and name[index] != ".":
                index += 1
            part = name[start:index].strip()
            if any(
                char.isspace() or char in {'"', "`", "[", "]", "'"} for char in part
            ):
                raise ValueError("SQL identifiers containing delimiters must be quoted")
        _require_identifier(part)
        parts.append(part)
        if index < len(name):
            index += 1
            if index == len(name):
                raise ValueError("SQL name has an empty identifier")
    return parts


def parse_table(name: str, config: Config, dialect: str = "") -> TableName:
    """Parse one to three SQL parts; incomplete names require explicit defaults.

    Standard double quotes, MySQL backticks, and SQL Server brackets are accepted
    independently of the optional dialect, without importing a SQL parser.
    """
    parts = _split_sql_name(name)
    if len(parts) > 3:
        raise ValueError("SQL table names may contain at most three identifier parts")
    if len(parts) == 3:
        database, schema, table = parts
    elif len(parts) == 2:
        database = config.default_database
        schema, table = parts
    else:
        database, schema, table = (
            config.default_database,
            config.default_schema,
            parts[0],
        )
    if database is None or schema is None:
        raise ValueError(
            "incomplete SQL table names require explicit default_database and default_schema"
        )
    return TableName(database, schema, table)
