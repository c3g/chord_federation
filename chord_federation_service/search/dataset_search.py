import itertools
import json

from chord_lib.responses.errors import bad_request_error, internal_server_error
from chord_lib.search.data_structure import check_ast_against_data_structure
from chord_lib.search.queries import convert_query_to_ast_and_preprocess, Query
from collections.abc import Iterable
from datetime import datetime
from tornado.httpclient import AsyncHTTPClient, HTTPError
from tornado.netutil import Resolver
from tornado.web import RequestHandler

from typing import Any, Dict, Iterable as TypingIterable, List, Optional, Set, Tuple

from ..constants import CHORD_HOST, MAX_BUFFER_SIZE, SERVICE_NAME, SOCKET_INTERNAL_URL
from ..utils import peer_fetch, ServiceSocketResolver, get_request_json


AsyncHTTPClient.configure(None, max_buffer_size=MAX_BUFFER_SIZE, resolver=ServiceSocketResolver(resolver=Resolver()))


__all__ = [
    "DatasetSearchHandler",
    "PrivateDatasetSearchHandler",
]


DATASET_SEARCH_HEADERS = {"Host": CHORD_HOST}


FieldSpec = List[str]
DataTypeAndField = Tuple[str, FieldSpec]
DictOfDataTypesAndFields = Dict[str, FieldSpec]
LinkedFieldSetList = List[DictOfDataTypesAndFields]


def _linked_fields_to_join_query_fragment(field_1: DataTypeAndField, field_2: DataTypeAndField) -> Query:
    return ["#eq", ["#resolve", field_1[0], "[item]", *field_1[1]], ["#resolve", field_2[0], "[item]", *field_2[1]]]


def _linked_field_set_to_join_query_rec(pairs: tuple) -> Query:
    if len(pairs) == 1:
        return _linked_fields_to_join_query_fragment(*pairs[0])

    return ["#and",
            _linked_fields_to_join_query_fragment(*pairs[0]),
            _linked_field_set_to_join_query_rec(pairs[1:])]


def _linked_field_sets_to_join_query(linked_field_sets: LinkedFieldSetList, data_type_set: Set[str]) -> Optional[Query]:
    if len(linked_field_sets) == 0:
        return None

    # TODO: This blows up combinatorially, oh well.
    pairs = tuple(p for p in itertools.combinations(linked_field_sets[0].items(), 2)
                  if p[0][0] in data_type_set and p[1][0] in data_type_set)

    if len(pairs) == 0:
        return None  # TODO: Somehow tell the user no join was applied or return NO RESULTS if None and 2+ data types?

    if len(linked_field_sets) == 1:
        return _linked_field_set_to_join_query_rec(pairs)

    return ["#and",
            _linked_field_set_to_join_query_rec(pairs),
            _linked_field_sets_to_join_query(linked_field_sets[1:], data_type_set)]


def _augment_resolves(query: Query, prefix: Tuple[str, ...]) -> Query:
    if not isinstance(query, list) or len(query) == 0 or len(query[0]) == 0 or query[0][0] != "#":
        return query

    if query[0] == "#resolve":
        return ["#resolve", *prefix, *query[1:]]

    return [query[0], *(_augment_resolves(q, prefix) for q in query[1:])]


def _get_array_resolve_paths(query: Query) -> List[str]:
    """
    Collect string representations array resolve paths without the trailing [item] resolution from a query. This can
    facilitate determining which index combinations will appear; and can be used as a step in filtering results by
    index combination.
    :param query: Query to collect array resolves from
    :return: List of index combination-compatible array resolve paths.
    """

    if isinstance(query, list) and len(query) > 1:
        r = []

        if query[0] == "#resolve":  # Resolve expression; items make up a resolve path
            r = []
            path = "_root"
            for ri in query[1:]:
                if ri == "[item]":
                    r.append(path)
                path = f"{path}.{ri}"

        else:  # Expression where items are other expressions/literals
            for e in query[1:]:
                r.extend(_get_array_resolve_paths(e))

        return r

    return []


class Kept:
    def __init__(self, data):
        self.data = data.data if isinstance(data, Kept) else data

    def __getitem__(self, item):
        return self.data[item]

    def __iter__(self):
        yield from self.data


def _filter_kept(data_structure: Any, ic_path: List[str]) -> Any:
    if not ic_path:
        return data_structure

    if isinstance(data_structure, list):
        return [Kept(_filter_kept(i.data, ic_path[1:])) for i in data_structure if isinstance(i, Kept)]

    return _filter_kept(data_structure[ic_path[0]], ic_path[1:])


def _strip_kept(data_structure: Any, ic_path: List[str]) -> Any:
    if not ic_path:
        return data_structure

    if isinstance(data_structure, list):
        return [_strip_kept(i.data if isinstance(i, Kept) else i, ic_path[1:]) for i in data_structure]

    return _strip_kept(data_structure[ic_path[0]], ic_path[1:])


def process_dataset_results(
    data_type_queries: Dict[str, Query],
    dataset_join_query: Query,
    dataset_results: Dict[str, list],
    dataset: dict,
    dataset_object_schema: dict,
    include_internal_data: bool,
    ic_paths_to_filter: Optional[List[str]] = None,
):
    # TODO: Check dataset, table-level authorizations

    # dataset_id: dataset identifier
    # dataset_results: dict of data types and corresponding table matches

    # TODO: Avoid re-compiling a fixed join query
    join_query_ast = convert_query_to_ast_and_preprocess(dataset_join_query) if dataset_join_query is not None else None

    print(f"[{SERVICE_NAME} {datetime.now()}] Compiled join query: {join_query_ast}", flush=True)

    # Truth-y if:
    #  - include_internal_data = False and check_ast_against_data_structure returns True
    #  - include_internal_data = True and check_ast_against_data_structure doesn't return an empty iterable
    ic = None
    if join_query_ast is not None:
        ic = check_ast_against_data_structure(join_query_ast, dataset_results, dataset_object_schema,
                                              internal=True, return_all_index_combinations=include_internal_data)
        if isinstance(ic, Iterable):
            ic = tuple(ic)

    # Append result if:
    #  - No join query was specified,
    #      and there is at least one matching table present in the dataset,
    #      and only one data type is being searched; or
    #  - A join query is present and evaluates to True against the dataset.
    # Need to mark this query as internal, since the federation service "gets" extra privileges here
    # (joined data isn't explicitly exposed.)
    # TODO: Optimize by not fetching if the query isn't going anywhere (i.e. no linked field sets, 2+ data types)
    if ((join_query_ast is None and any(len(dtr) > 0 for dtr in dataset_results.values())
         and len(data_type_queries) == 1) or (join_query_ast is not None and ic)):

        ic_paths_to_filter_set = set(ic_paths_to_filter)

        # TODO: This stuff is slow

        for index_combination in ic:
            resolved_versions = {}

            for path, index in sorted(index_combination.items(), key=lambda pair: len(pair[0])):
                if path not in ic_paths_to_filter_set:
                    continue

                path_array_parts = path.split(".[item]")

                resolved_path = ""
                current_path = ""
                for p in path_array_parts[:-1]:
                    current_path += p
                    resolved_path += resolved_versions[current_path]

                resolved_path += f"{path_array_parts[-1]}.[{index}]"
                resolved_versions[path] = resolved_path

            for resolved_path in resolved_versions.values():
                path_parts = resolved_path.split(".")[1:]
                ds: Any = dataset_results
                for pp in path_parts:
                    arr = pp[0] == "["
                    idx = int(pp[1:-1]) if arr else pp
                    if arr:
                        ds[idx] = Kept(ds[idx])
                    ds = ds[idx]

        sorted_icps = sorted(ic_paths_to_filter, key=lambda icp: len(icp))

        for ic_path in sorted_icps:
            dataset_results = _filter_kept(dataset_results, ic_path.split(".")[1:])

        for ic_path in sorted_icps:
            dataset_results = _strip_kept(dataset_results, ic_path.split(".")[1:])

        yield {
            **dataset,
            **({"results": dataset_results,  # TODO: Filter this!
                "index_combinations": ic} if include_internal_data else {})
        }  # TODO: Make sure all information here is public-level if include_internal_data is False.


def _get_dataset_linked_field_sets(dataset: dict) -> LinkedFieldSetList:
    return [
        lfs["fields"]
        for lfs in dataset.get("linked_field_sets", [])
        if len(lfs["fields"]) > 1  # Only include useful linked field sets, i.e. 2+ fields
    ]


async def run_search_on_dataset(
    client: AsyncHTTPClient,
    dataset_object_schema: dict,
    dataset: dict,
    join_query: Query,
    data_type_queries: Dict[str, Query],
    include_internal_results: bool,
) -> Tuple[Dict[str, list], Query, List[str]]:
    linked_field_sets: LinkedFieldSetList = _get_dataset_linked_field_sets(dataset)
    dataset_join_query = join_query

    if dataset_join_query is None:
        # Could re-return None; pass set of all data types (keys of the data type queries) to filter out combinations
        dataset_join_query = _linked_field_sets_to_join_query(linked_field_sets, set(data_type_queries))

    ic_paths_to_filter = _get_array_resolve_paths(dataset_join_query) if include_internal_results else []

    if dataset_join_query is not None:  # still isn't None...
        # TODO: Pre-filter data_type_results to avoid a billion index combinations - return specific set of
        #  combos
        # TODO: Allow passing a non-empty index fixation to search to save time and start somewhere
        # TODO: Or should search filter the data object (including sub-arrays) as it goes, returning it at
        #  the end?

        # Combine the join query with data type queries to be able to link across fixed [item]s
        for dt, q in data_type_queries.items():
            dataset_join_query = ["#and", _augment_resolves(q, (dt, "[item]")), dataset_join_query]

        print(f"[{SERVICE_NAME} {datetime.now()}] Generated join query: {dataset_join_query}", flush=True)

    dataset_results = {}

    for t in dataset["table_ownership"]:
        # TODO: Don't fetch schema
        table_record = await peer_fetch(
            client,
            SOCKET_INTERNAL_URL,  # Use Unix socket resolver
            f"api/{t['service_artifact']}/tables/{t['table_id']}",
            method="GET",
            extra_headers=DATASET_SEARCH_HEADERS
        )

        table_id = table_record["id"]
        table_data_type = table_record["data_type"]
        table_service_artifact = t["service_artifact"]

        if table_data_type not in dataset_results:
            dataset_results[table_data_type] = []

        if dataset_join_query is not None:  # still isn't None...
            if table_data_type not in dataset_object_schema["properties"]:
                # Fetch schema for data type if needed
                dataset_object_schema["properties"][table_data_type] = {
                    "type": "array",
                    "items": table_record["schema"] if table_data_type in data_type_queries else {}
                }

            # TODO: We should only fetch items that match including sub-items (e.g. limited calls) by using
            #  all index combinations that match and combining them... something like that

            dataset_results[table_data_type].extend((await peer_fetch(
                client,
                SOCKET_INTERNAL_URL,  # Use Unix socket resolver
                f"api/{table_service_artifact}/private/tables/{table_id}/search",
                request_body=json.dumps({"query": data_type_queries[table_data_type]}),
                method="POST",
                extra_headers=DATASET_SEARCH_HEADERS
            ))["results"] if table_data_type in data_type_queries else [])

        elif table_data_type in data_type_queries:
            # Don't need to fetch results for joining if the join query is None; just check
            # individual tables (which is much faster) using the public discovery endpoint.

            r = await peer_fetch(
                client,
                SOCKET_INTERNAL_URL,  # Use Unix socket resolver
                path_fragment=(
                    f"api/{table_service_artifact}/{'private/' if include_internal_results else ''}"
                    f"tables/{table_id}/search"
                ),
                request_body=json.dumps({"query": data_type_queries[table_data_type]}),
                method="POST",
                extra_headers=DATASET_SEARCH_HEADERS
            )

            if not include_internal_results:
                # Here, the array of 1 True is a dummy value to give a positive result
                r = [r] if r else []
            else:
                # We have a results array to account for
                r = r["results"]

            if len(r) > 0:  # True return value, i.e. the query matched something
                dataset_results[table_data_type].extend(r)

    # Return dataset-level results to calculate final result from
    # Return dataset join query for later use (when generating results)
    # Return index combination paths to filter by (for returning a proper result-set)
    return dataset_results, dataset_join_query, ic_paths_to_filter


def get_query_parts(request_body: bytes) -> Tuple[Optional[Dict[str, Query]], Optional[Query]]:
    request = get_request_json(request_body)
    if request is None:
        return None, None

    # Format: {"data_type": ["#eq", ...]}
    data_type_queries: Optional[Dict[str, Query]] = request.get("data_type_queries")

    # Format: normal query, using data types for join conditions
    join_query: Optional[Query] = request.get("join_query")

    return data_type_queries, join_query


def test_queries(queries: TypingIterable[Query]) -> None:
    """
    Throws an error if a query in the iterable cannot be compiled.
    :param queries: Iterable of queries to attempt compilation of.
    :return: None
    """
    for q in queries:
        # Try compiling each query to make sure it works.
        convert_query_to_ast_and_preprocess(q)


# noinspection PyAbstractClass
class DatasetSearchHandler(RequestHandler):  # TODO: Move to another dedicated service?
    """
    Aggregates tables into datasets and runs a query against the data. Does not reveal internal object-level data.
    """

    include_internal_results = False

    async def options(self):
        self.set_status(204)
        await self.finish()

    async def post(self):
        data_type_queries, join_query = get_query_parts(self.request.body)
        if not data_type_queries:
            self.set_status(400)
            self.write(bad_request_error("Invalid request format (missing body or data_type_queries)"))
            return

        results = []

        try:
            # Try compiling each query to make sure it works. Any exceptions thrown will get caught below.
            test_queries(data_type_queries.values())

            client = AsyncHTTPClient()

            # TODO: Handle pagination
            # TODO: Why fetch projects instead of datasets? Is it to avoid "orphan" datasets? Is that even possible?
            # Use Unix socket resolver

            projects = await peer_fetch(client, SOCKET_INTERNAL_URL, "api/metadata/api/projects", method="GET",
                                        extra_headers=DATASET_SEARCH_HEADERS)

            datasets_dict: Dict[str, dict] = {d["identifier"]: d for p in projects["results"] for d in p["datasets"]}
            dataset_objects_dict: Dict[str, Dict[str, list]] = {d: {} for d in datasets_dict}

            dataset_object_schema = {
                "type": "object",
                "properties": {}
            }

            dataset_join_queries: Dict[str, Query] = {d: None for d in datasets_dict}

            for dataset_id, dataset in datasets_dict.items():  # TODO: Worker
                dataset_results, dataset_join_query, _ = await run_search_on_dataset(
                    client,
                    dataset_object_schema,
                    datasets_dict[dataset_id],
                    join_query,
                    data_type_queries,
                    self.include_internal_results,
                )

                dataset_objects_dict[dataset_id] = dataset_results
                dataset_join_queries[dataset_id] = dataset_join_query

            print(f"[{SERVICE_NAME} {datetime.now()}] Done fetching individual service search results.", flush=True)

            # Aggregate datasets into results list if they satisfy the queries
            for dataset_id, dataset_results in dataset_objects_dict.items():  # TODO: Worker
                results.extend(process_dataset_results(
                    data_type_queries,
                    dataset_join_queries[dataset_id],
                    dataset_results,
                    datasets_dict[dataset_id],
                    dataset_object_schema,
                    include_internal_data=False
                ))

            self.write({"results": results})

        except HTTPError as e:
            # Metadata service error
            print(f"[{SERVICE_NAME} {datetime.now()}] Error from service: {str(e)}", flush=True)  # TODO: Better message
            self.set_status(500)
            self.write(internal_server_error(f"Error from service: {str(e)}"))

        # except (TypeError, ValueError, SyntaxError) as e:  # errors from query processing
        #     # TODO: Better / more compliant error message
        #     # TODO: Move these up?
        #     # TODO: Not guaranteed to be actually query-processing errors
        #     print(str(e))
        #     self.set_status(400)
        #     self.write(bad_request_error(f"Query processing error: {str(e)}"))  # TODO: Better message


# noinspection PyAbstractClass
class PrivateDatasetSearchHandler(RequestHandler):
    include_internal_results = True

    async def options(self, _dataset_id: str):
        self.set_status(204)
        await self.finish()

    async def post(self, dataset_id: str):
        data_type_queries, join_query = get_query_parts(self.request.body)
        if not data_type_queries:
            self.set_status(400)
            self.write(bad_request_error("Invalid request format (missing body or data_type_queries)"))
            return

        try:
            # Try compiling each query to make sure it works. Any exceptions thrown will get caught below.
            test_queries(data_type_queries.values())

            client = AsyncHTTPClient()

            # TODO: Handle dataset 404 properly

            dataset = await peer_fetch(
                client,
                SOCKET_INTERNAL_URL,
                f"api/metadata/api/datasets/{dataset_id}",
                method="GET",
                extra_headers=DATASET_SEARCH_HEADERS
            )

            dataset_object_schema = {
                "type": "object",
                "properties": {}
            }

            dataset_results, dataset_join_query, ic_paths_to_filter = await run_search_on_dataset(
                client,
                dataset_object_schema,
                dataset,
                join_query,
                data_type_queries,
                self.include_internal_results
            )

            self.write(next(process_dataset_results(
                data_type_queries,
                dataset_join_query,
                dataset_results,
                dataset,
                dataset_object_schema,
                include_internal_data=True,
                ic_paths_to_filter=ic_paths_to_filter,
            ), None))

            self.set_header("Content-Type", "application/json")

        except HTTPError as e:
            # Metadata service error
            print(f"[{SERVICE_NAME} {datetime.now()}] Error from service: {str(e)}", flush=True)  # TODO: Better message
            self.set_status(500)
            self.write(internal_server_error(f"Error from service: {str(e)}"))

        except (TypeError, ValueError, SyntaxError) as e:  # errors from query processing
            # TODO: Better / more compliant error message
            # TODO: Move these up?
            # TODO: Not guaranteed to be actually query-processing errors
            print(str(e))
            self.set_status(400)
            self.write(bad_request_error(f"Query processing error: {str(e)}"))  # TODO: Better message
