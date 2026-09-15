"""The review node loops on uncertainty, inside the DAG."""

from harness_fleet import contracts, dag


def test_the_contract_says_what_to_search_for_each_gap():
    """A gap in evidence becomes a search, and the rule is central."""
    queries = contracts.queries_for_gaps("acme.com", ["independent_validation", "delivery_hiring"])
    assert len(queries) == 2
    assert all("acme.com" in query for query in queries)
    assert "case study" in queries[0]
    assert contracts.queries_for_gaps("acme.com", ["no_such_kind"]) == []


def test_a_review_node_validates_and_is_not_a_cycle():
    spec = dag.DagSpec(
        name="review-demo",
        nodes=[
            dag.RunNode(id="score", task="demo", input="items.csv"),
            dag.ReviewNode(id="fill-gaps", from_run="score", tier="tier_1", max_rounds=2),
        ],
    )
    assert spec.topo_order() == ["score", "fill-gaps"]


def test_a_review_node_only_needs_the_run_it_reads():
    """It takes no ids_from and cannot introduce a cycle into the spec."""
    node = dag.ReviewNode(id="r", from_run="anywhere", max_rounds=1)
    assert node.kind == "review"
    assert node.require_kinds == [] and node.tier == "tier_1"
    assert dag.ReviewNode(id="r2", from_run="x", require_kinds=["delivery_proof"]).require_kinds == [
        "delivery_proof"
    ]
