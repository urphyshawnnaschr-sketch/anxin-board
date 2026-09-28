"""Local selection accounting, never a claim that a model analyzed the repository."""


def retrieval_coverage(repo_items, batches):
    inventory = {item['evidence_id']: item['content'] for item in repo_items}
    if len(inventory) != len(repo_items):
        raise ValueError('RETRIEVAL_COVERAGE_IDENTITY_MISMATCH')
    selected = set()
    for batch in batches:
        for item in batch['repo_evidence']:
            identity = item['evidence_id']
            if identity not in inventory or item['content'] != inventory[identity]:
                raise ValueError('RETRIEVAL_COVERAGE_IDENTITY_MISMATCH')
            selected.add(identity)
    missing = set(inventory) - selected
    return dict(
        safe_evidence_chunks=len(inventory), selected_unique_chunks=len(selected),
        unselected_safe_chunks=len(missing),
        selected_unique_safe_bytes=sum(len(inventory[key].encode('utf-8')) for key in selected),
        unselected_safe_bytes=sum(len(inventory[key].encode('utf-8')) for key in missing),
        full_safe_text_selected=bool(inventory) and not missing,
        model_analysis_complete=False,
    )
