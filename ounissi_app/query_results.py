"""Helpers for the common result shape returned by Xpert and internal queries."""


def column_values(result, column):
    """Return a named column without relying on column order or pandas objects.

    Do not silently turn a failed query or a malformed result into an empty
    list: callers need to distinguish unavailable data from a valid zero rows.
    """
    if not isinstance(result, dict) or result.get('error'):
        raise ValueError('La requête n’a pas pu être exécutée.')
    try:
        index = result['columns'].index(column)
        rows = result['data_rows']
        return [row[index] for row in rows]
    except (KeyError, ValueError, IndexError, TypeError) as exc:
        raise ValueError(f'La requête ne contient pas la colonne attendue : {column}.') from exc
