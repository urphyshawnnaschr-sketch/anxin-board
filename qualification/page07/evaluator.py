"""Read-only closure of reviewed synthetic evidence. Never supplies Human scores."""
import argparse
from pathlib import Path
from .common import digest
from .receipts import evaluate_run, read_json
from app.model_qualification_harness import load_base_sample_pack, evaluate_qualification_run, REPEAT_REPRESENTATIVE_SAMPLE_IDS
METRICS = tuple("git_objective_stats_correct no_fabrication main_feature_correct stage_correct implementation_scope_correct conservative_when_evidence_limited structural_integrity source_distinction_correct boundary_compliance".split())
SPECIAL = ("correction_handled_or_reasonably_refused", "unaffected_correct_facts_preserved", "no_new_unsupported_claims")

def merge(manifest, sidecar, directory, reviews, *, pin):
    structural=evaluate_run(manifest,sidecar,directory,expected_manifest_hash=pin)
    if not structural["all_slots_complete"] or not structural["structural_checks_passed"]:
        raise ValueError("COMPLETE_STRUCTURAL_EVIDENCE_REQUIRED")
    base=load_base_sample_pack()
    if sidecar["base_sample_manifest_hash"] != base["sample_manifest_hash"]:
        raise ValueError("BASE_MANIFEST_DRIFT")
    slots={s["slot_id"]:s for s in manifest["slots"]}
    hashes={a["slot_id"]:a["response_hash"] for a in structural["assessments"]}
    deterministic={a["slot_id"]:a["assessment"] for a in structural["assessments"]}
    accepted={}; excluded=[]
    for review in reviews:
        if review.get("manifest_pin")!=pin or review.get("source_identity")!=manifest["source_identity"]:
            raise ValueError("REVIEW_RUN_IDENTITY_INVALID")
        kind=review.get("reviewer_kind","")
        if not isinstance(kind,str) or not kind.startswith("independent_agent") or "builder" in kind:
            excluded.append(digest(review)); continue
        if review.get("human_readability_reviews") is not None:
            raise ValueError("HUMAN_REVIEWS_NOT_ACCEPTED_BY_THIS_TOOL")
        for section,phase in (("base_samples","base"),("repeat_samples","repeat")):
            for row in review.get(section,[]):
                sid=row.get("slot_id"); slot=slots.get(sid)
                if slot is None or slot["phase"]!=phase or row.get("response_hash")!=hashes[sid]:
                    raise ValueError("REVIEW_RECEIPT_BINDING_INVALID")
                if sid in accepted: raise ValueError("DUPLICATE_SLOT_REVIEW")
                obs=row.get("machine_observation",{})
                if set(obs)!={"sample_id","hard_failures",*METRICS} or obs["sample_id"]!=slot["sample_id"] or any(type(obs[k]) is not bool for k in METRICS):
                    raise ValueError("OBSERVATION_INVALID")
                for metric,check in (("stage_correct","stage_allowed"),("implementation_scope_correct","implementation_scope_allowed"),("structural_integrity","local_structural_checks_passed")):
                    if obs[metric] is True and deterministic[sid][check] is not True: raise ValueError("REVIEW_DETERMINISTIC_INCONSISTENCY")
                if type(obs['hard_failures']) is not list or any(type(v) is not str or not v.strip() for v in obs['hard_failures']): raise ValueError("HARD_FAILURES_INVALID")
                if any(type(row.get(k)) is not bool for k in SPECIAL): raise ValueError("SPECIAL_REVIEW_MISSING")
                if phase=='repeat':
                    core=row.get('repeat_core',{})
                    if set(core)!={'main_feature','stage','implementation_scope','contradiction'} or type(core['contradiction']) is not bool or any(type(core[k]) is not str or not core[k].strip() for k in ('main_feature','stage','implementation_scope')): raise ValueError('REPEAT_CORE_MISSING')
                if phase=='repeat':
                    receipt=read_json(Path(directory)/(sid+'.receipt.json'))
                    if receipt.get('response_hash')!=hashes[sid] or digest({k:v for k,v in receipt.items() if k!='response_hash'})!=hashes[sid]: raise ValueError('REPEAT_RECEIPT_DRIFT')
                    progress=receipt.get('result',{}).get('new_report',{}).get('feature_progress')
                    if type(progress) is not list or len(progress)!=1: raise ValueError('REPEAT_SINGLE_MODULE_REQUIRED')
                    actual={'main_feature':progress[0].get('feature'),'stage':progress[0].get('stage'),'implementation_scope':progress[0].get('implementation_scope')}
                    if any(core[k]!=v for k,v in actual.items()): raise ValueError('REPEAT_CORE_RECEIPT_MISMATCH')
                accepted[sid]=row
    missing=[sid for sid in slots if sid not in accepted]
    output={"schema_version":"private_merged_semantic_evaluation_v1","manifest_pin":pin,"source_identity":manifest['source_identity'],"structural_evaluation_hash":structural['evaluation_hash'],"review_hashes":[digest(r) for r in reviews],"excluded_nonindependent_review_hashes":excluded,"missing_independent_reviews":missing,"human_readability_reviews":None,"automatic_admission":False,"qualification_status":"not_evaluated","machine_gate_passed":None,"original_harness":None}
    if not missing:
        observations=[accepted[s['slot_id']]['machine_observation'] for s in manifest['slots'] if s['phase']=='base']
        repeat={sid:[accepted[s['slot_id']]['repeat_core'] for s in manifest['slots'] if s['phase']=='repeat' and s['sample_id']==sid] for sid in REPEAT_REPRESENTATIVE_SAMPLE_IDS}
        evaluated=evaluate_qualification_run(observations=observations,repeat_runs=repeat,readability_reviews=None,pack=base)
        output.update(original_harness=evaluated,machine_gate_passed=evaluated['machine_gate_passed'],qualification_status=evaluated['qualification_status'],task_specific_gate_passed=all(row[k] for row in accepted.values() for k in SPECIAL),repeat_observations=[accepted[s['slot_id']]['machine_observation'] for s in manifest['slots'] if s['phase']=='repeat'])
        repeat_hard_metrics=('git_objective_stats_correct','no_fabrication','structural_integrity','source_distinction_correct','boundary_compliance')
        output['repeat_semantic_hard_gate_passed']=all(not obs['hard_failures'] and all(obs[k] for k in repeat_hard_metrics) for obs in output['repeat_observations'])
        output['repeat_semantic_hard_gate_note']='Additional reviewed repeat safety closure; original harness only tests repeat triple stability/contradiction and its result remains unchanged.'
        output['combined_machine_and_task_specific_passed']=output['machine_gate_passed'] and output['task_specific_gate_passed'] and output['repeat_semantic_hard_gate_passed']
        if not output['combined_machine_and_task_specific_passed']: output['qualification_status']='not_qualified'
    output['evaluation_hash']=digest(output)
    return output

STANDARD_VERSION = "page07-regenerate-compatibility/2.0"
PACK_VERSION = "page07-regenerate-qualification-pack/2.0"
RULE_VERSION = "page07-regenerate-rules/2.0"

def current_identity(capability):
    from app.ai_contracts import AI_CONTRACT_SCHEMA_VERSION
    from app.model_gateway import _regenerate_prompt_contract, _regenerate_sampling_policy
    return {"provider": capability["provider"], "model_id": capability["model_id"],
            "model_version": capability["model_version"], "task_type": "daily_report_regenerate",
            "ai_contract_schema_version": AI_CONTRACT_SCHEMA_VERSION,
            "output_schema_version": "daily-report-regenerate/1.0",
            "prompt_version": "page07-regenerate-prompt/1.0",
            "prompt_contract_hash": _regenerate_prompt_contract()[1],
            "sampling_parameters_hash": _regenerate_sampling_policy()[1],
            "rule_version": RULE_VERSION, "sample_pack_version": PACK_VERSION}

def evaluate_compatibility(manifest, sidecar, directory, reviews, *, pin, current_capability):
    """Evaluate reviewed actual receipts. No network, registry or DB writes.

    Independent reviewers attest semantic observations; hashes bind these
    observations to receipts, but cannot prove reviewer independence or truth.
    Final provenance review and explicit registry adoption remain external.
    """
    expected = current_identity(current_capability)
    if manifest.get("capability") != current_capability or manifest.get("qualification_identity") != expected:
        raise ValueError("CURRENT_QUALIFICATION_IDENTITY_MISMATCH")
    for request in manifest.get("requests", {}).values():
        if any(request.get("plan", {}).get(k) != expected[k] for k in
               ("prompt_version", "prompt_contract_hash", "sampling_parameters_hash")):
            raise ValueError("REQUEST_POLICY_IDENTITY_MISMATCH")
    frozen = read_json(Path(__file__).with_name("samples.json"))
    if sidecar != frozen:
        raise ValueError("FROZEN_SAMPLE_PACK_MISMATCH")
    value = merge(manifest, sidecar, directory, reviews, pin=pin)
    value.update(schema_version=STANDARD_VERSION, sample_pack_version=PACK_VERSION,
                 rule_version=RULE_VERSION, human_readability_certified=False,
                 compatibility_passed=value.get("combined_machine_and_task_specific_passed") is True,
                 automatic_admission=False, final_report_human_confirmation_required=True)
    # qualification_status and original_harness preserve the original Human gate.
    value.pop("evaluation_hash", None)
    value["evaluation_hash"] = digest(value)
    return value
