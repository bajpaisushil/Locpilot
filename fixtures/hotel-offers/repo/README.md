# Hotel offers service

Aggregates hotel room offers from several suppliers (`offers/suppliers.py`), merges them
(`offers/aggregator.py`) and returns them from the search endpoint (`offers/search.py`).

Run the tests with `python3 -m unittest discover -s tests -t .`
