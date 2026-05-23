"""Run GraphQL introspection against Polestar's mystar-v2 endpoint.

Uses the app's existing Settings (so credentials come from .env) and writes
the full schema to /tmp/polestar_schema.json. No credentials hit stdout.
"""
import asyncio
import json
import sys

from gql import gql
from pypolestar import PolestarApi

from polestar_dash.config import get_settings


INTROSPECTION = gql(
    """
    query IntrospectionQuery {
      __schema {
        queryType { name }
        mutationType { name }
        types {
          kind
          name
          description
          fields(includeDeprecated: true) {
            name
            description
            args { name type { ...TypeRef } defaultValue }
            type { ...TypeRef }
            isDeprecated
            deprecationReason
          }
          inputFields { name type { ...TypeRef } defaultValue }
          interfaces { ...TypeRef }
          enumValues(includeDeprecated: true) { name description }
          possibleTypes { ...TypeRef }
        }
      }
    }
    fragment TypeRef on __Type {
      kind name
      ofType {
        kind name
        ofType {
          kind name
          ofType { kind name ofType { kind name } }
        }
      }
    }
    """
)


async def main() -> int:
    s = get_settings()
    api = PolestarApi(
        username=s.polestar_email,
        password=s.polestar_password,
        vins=s.vin_list or None,
    )
    await api.async_init()
    try:
        result = await api._query_graph_ql(query=INTROSPECTION)
    except Exception as e:
        print(f"private API introspection failed: {e}", file=sys.stderr)
        try:
            result = await api._query_graph_ql(query=INTROSPECTION, public_api=True)
            print("(fell back to public API)", file=sys.stderr)
        except Exception as e2:
            print(f"public API introspection also failed: {e2}", file=sys.stderr)
            return 1
    with open("/tmp/polestar_schema.json", "w") as f:
        json.dump(result, f, indent=2)
    types = result["__schema"]["types"]
    print(f"OK: {len(types)} types written to /tmp/polestar_schema.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
