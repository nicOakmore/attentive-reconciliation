"""The cause catalogue, in one readable place. Running this file regenerates
services/cause_rules.json, the decision table the audit runs every employee through.

Design rules for the catalogue:
  - every row decides one cause from evidence the audit computed; no row computes evidence
  - wording never mentions tax tables; an out-of-date report is called an out-of-date report
  - every actionable cause ends with what to do
  - a row only fires when its evidence is actually present, never on a missing read
"""
import json
import os

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   'services', 'cause_rules.json')

IN = ['ret_named_known', 'ret_missing_abs', 'ret_unnamed_abs', 'ret_zero', 'ti_gap', 'wrong_col_match',
      'slips_present', 'ss_on_slips', 'eng_ss', 'eng_ss_known', 'pay_ss_sav', 'fee_known', 'fee_gap_abs',
      'eng_ti_known', 'eng_ti_zero', 'census_gross', 'premium_known', 'premium_gap_abs', 'fixed_fed',
      'fed_zero', 'w4_diff', 'gross_moved_abs', 'identity_broken', 'identity_explained', 'fee_from_stmt',
      'fed_in_doubt', 'federal_gap_abs', 'state_gap_abs', 'state_is_mo', 'fica_gap_abs', 'ss_mismatch',
      # payroll execution and report integrity
      'pretax_known', 'premium_pretax_gap_abs', 'medg_known', 'medg_gap_abs', 'reimb_missing_sig',
      'reimb_known', 'reimb_gap_abs', 'med_read', 'med_on_slips', 'eng_med', 'eng_med_known',
      'pay_med_sav', 'med_mismatch', 'ret_change_abs', 'report_int_known', 'report_int_gap_abs',
      'census_found', 'census_ss', 'census_med']
OUTS = ['label', 'amount', 'detail']

Y = {"operator": "=", "value": "Y"}
N = {"operator": "=", "value": "N"}


def gt(v):
    return {"operator": ">", "value": v}


def lt(v):
    return {"operator": "<", "value": v}


def lte(v):
    return {"operator": "<=", "value": v}


def row(name, conds, label, amount, detail):
    cells = [{"column": "in_" + k, "scalarCondition": c} for k, c in conds.items()]
    cells.append({"column": "out_label", "outputScalarValue": {"type": "common", "value": label}})
    cells.append({"column": "out_amount", "outputScalarValue":
                  ({"type": "common", "value": None} if amount is None
                   else {"type": "function", "value": amount})})
    cells.append({"column": "out_detail", "outputScalarValue": {"type": "common", "value": detail}})
    return {"name": name, "active": True, "cells": cells}


RET_TAIL = " Put it in the census 401-k/IRA column and rerun the proposal."

ROWS = [
    # ---- census content -----------------------------------------------------------------
    row("retirement named, fully explained",
        {"ret_named_known": Y, "ret_missing_abs": gt(0.02), "ret_unnamed_abs": lte(0.02)},
        "Retirement deduction missing from the census", "{ret_missing}", "ret_named_exact"),
    row("retirement named, with a further unnamed pre-tax deduction",
        {"ret_named_known": Y, "ret_missing_abs": gt(0.02), "ret_unnamed_abs": gt(0.02)},
        "Retirement deduction missing from the census", "{ret_missing}", "ret_named_plus"),
    row("pre-tax reduction of federal wages the statement does not name",
        {"ret_named_known": N, "ret_missing_abs": gt(0.02)},
        "A pre-tax deduction is missing from the census", "{ret_missing}", "ret_unnamed"),
    row("proposal starts from more income than the payslip",
        {"ti_gap": gt(0.02), "ret_zero": Y},
        "A pre-tax deduction is missing from the census", "{ti_gap}", "pretax_missing"),
    row("a deduction was entered in the wrong census column",
        {"ti_gap": lt(-1.0), "wrong_col_match": Y},
        "A deduction sits in the wrong census column", "{ti_gap}", "wrong_column"),
    row("proposal starts from less income than the payslip, cause unknown",
        {"ti_gap": lt(-1.0), "wrong_col_match": N},
        "The proposal starts from less income than the payslip shows", "{ti_gap}", "ti_neg"),
    row("no census row matches this employee",
        {"census_found": N, "slips_present": Y},
        "No census row matches this employee", None, "census_missing"),
    # ---- programme settings -------------------------------------------------------------
    row("Social Security savings claimed on a payroll that never deducts it",
        {"slips_present": Y, "ss_on_slips": N, "eng_ss": gt(0.02)},
        "The proposal counts Social Security savings this payroll never pays", "0 - {eng_ss}", "ss_on"),
    row("census SocialSec is not N on a payroll that deducts none",
        {"slips_present": Y, "ss_on_slips": N, "census_ss": {"operator": "!=", "value": "N"}, "eng_ss": lte(0.02)},
        "The census does not have Social Security set to N", None, "census_ss_not_n"),
    row("census SocialSec is N on a payroll that deducts it",
        {"slips_present": Y, "ss_on_slips": Y, "census_ss": {"operator": "=", "value": "N"}},
        "The census has Social Security set to N but payroll deducts it", None, "census_ss_not_y"),
    row("census Medicare is not N on a payroll that deducts none",
        {"slips_present": Y, "med_read": Y, "med_on_slips": N, "census_med": {"operator": "!=", "value": "N"},
         "eng_med": lte(0.02)},
        "The census does not have Medicare set to N", None, "census_med_not_n"),
    row("payroll deducts Social Security the proposal ignores",
        {"slips_present": Y, "ss_on_slips": Y, "eng_ss_known": Y, "eng_ss": lte(0.02)},
        "The payroll pays Social Security the proposal ignores", "{pay_ss_sav}", "ss_off"),
    row("Medicare savings claimed on a payroll that never deducts it",
        {"slips_present": Y, "med_read": Y, "med_on_slips": N, "eng_med": gt(0.02)},
        "The proposal counts Medicare savings this payroll never pays", "0 - {eng_med}", "med_on"),
    row("payroll deducts Medicare the proposal ignores",
        {"slips_present": Y, "med_read": Y, "med_on_slips": Y, "eng_med_known": Y, "eng_med": lte(0.02),
         "pay_med_sav": gt(0.02)},
        "The payroll pays Medicare the proposal ignores", "{pay_med_sav}", "med_off"),
    row("the fee in the proposal is not the fee payroll deducts",
        {"fee_known": Y, "fee_gap_abs": gt(0.02)},
        "The employee fee in the proposal is not the fee payroll deducts", "0 - {fee_gap}", "fee_mismatch"),
    row("the proposal calculated on no income at all",
        {"eng_ti_known": Y, "eng_ti_zero": Y, "census_gross": gt(0)},
        "The proposal calculated on no income at all", "{gap}", "salary_zero"),
    row("the premium on the payslip is not the premium in the proposal",
        {"premium_known": Y, "premium_gap_abs": gt(0.02)},
        "The premium on the payslip is not the premium in the proposal", "{premium_gap}", "premium_mismatch"),
    # ---- payroll execution --------------------------------------------------------------
    row("the premium was not taken pre-tax in full",
        {"pretax_known": Y, "premium_pretax_gap_abs": gt(1.0)},
        "The premium was not taken pre-tax in full", "{premium_pretax_gap}", "premium_not_pretax"),
    row("the premium did not come out of Medicare wages in full",
        {"medg_known": Y, "medg_gap_abs": gt(1.0)},
        "The premium did not come out of Medicare wages in full", "{medg_gap}", "premium_not_medicare"),
    row("the premium is deducted but never reimbursed",
        {"reimb_missing_sig": Y},
        "The premium is deducted but never reimbursed", "0 - {premium_m}", "reimb_missing"),
    row("the reimbursement does not return the whole premium",
        {"reimb_known": Y, "premium_known": Y, "reimb_gap_abs": gt(1.0)},
        "The reimbursement does not return the whole premium", "{reimb_gap}", "reimb_partial"),
    row("the retirement deduction changed with the premium",
        {"ret_change_abs": gt(1.0)},
        "The retirement deduction changed with the premium", "{ret_change}", "ret_changed"),
    row("payroll withholds a fixed federal amount",
        {"fixed_fed": Y},
        "Payroll withholds a fixed federal amount", "{fed_before_m}", "fixed_fed"),
    row("no federal tax left to save",
        {"fed_zero": Y},
        "No federal tax left to save", "{fed_before_m}", "fed_zero"),
    row("the W-4 on payroll differs from the census",
        {"w4_diff": Y},
        "The W-4 on payroll differs from the census", None, "w4_diff"),
    row("gross pay moved between the two payslips",
        {"gross_moved_abs": gt(0.02)},
        "Something else changed between the two payslips", "{gross_moved}", "gross_moved"),
    # ---- statement integrity ------------------------------------------------------------
    row("the statement does not add up, fee read from the statement",
        {"identity_broken": Y, "identity_explained": N, "fee_from_stmt": Y},
        "The statement does not add up", "{identity_gap}", "identity_fee_stmt"),
    row("the statement does not add up, fee taken from the proposal",
        {"identity_broken": Y, "identity_explained": N, "fee_from_stmt": N},
        "The statement does not add up", "{identity_gap}", "identity_fee_prop"),
    # ---- report integrity ---------------------------------------------------------------
    row("the proposal report does not add up internally",
        {"report_int_known": Y, "report_int_gap_abs": gt(0.02)},
        "The proposal report does not add up internally", "{report_int_gap}", "report_internal"),
    row("the proposal's federal saving does not match payroll",
        {"fed_in_doubt": N, "federal_gap_abs": gt(0.02)},
        "The proposal's federal saving does not match payroll", "{federal_gap}", "fed_mismatch"),
    # ---- residual component differences -------------------------------------------------
    row("state withholding, Missouri whole-dollar rounding",
        {"state_gap_abs": gt(0.02), "state_is_mo": Y},
        "State withholding", "{state_gap}", "state_mo"),
    row("state withholding",
        {"state_gap_abs": gt(0.02), "state_is_mo": N},
        "State withholding", "{state_gap}", "state_generic"),
    row("Social Security and Medicare differ, settings consistent",
        {"fica_gap_abs": gt(0.02), "ss_mismatch": N, "med_mismatch": N},
        "Social Security and Medicare", "{fica_gap}", "fica"),
]

DETAILS = {
    "ret_named_exact": "Payroll takes {ret_missing} a month pre-tax for retirement; the census does not carry it, so the proposal taxed income payroll does not tax." + RET_TAIL,
    "ret_named_plus": "Payroll reduces federal wages by {ret_missing} a month; the payslip names {ret_named} as retirement and does not name the other {ret_unnamed}. None of it is in the census." + RET_TAIL,
    "ret_unnamed": "Payroll reduces federal wages by this amount pre-tax; the census does not carry it and the payslip does not name it." + RET_TAIL,
    "pretax_missing": "The proposal starts from more income than the payslip taxes: a pre-tax deduction is missing from the census. Add it and rerun the proposal.",
    "wrong_column": "The proposal starts {ti_gap_abs} a month low: a retirement amount sits in the Other pre-tax column, which also cuts Social Security and Medicare wages. Move it to the 401-k/IRA column and rerun the proposal.",
    "ti_neg": "The proposal starts from less income than the payslip shows: a census pre-tax figure is too high or misplaced. Check fields Q and R against the payslip.",
    "census_missing": "No census row matches this employee. Add them to the census and rerun the proposal.",
    "ss_on": "The proposal counts {eng_ss} a month of Social Security savings; the payslips deduct none. Set SocialSec to N and rerun the proposal.",
    "ss_off": "The payslips deduct Social Security; the proposal counts no saving on it, leaving {pay_ss_sav} a month out of the promise. Set SocialSec to Y and rerun the proposal.",
    "med_on": "The proposal counts {eng_med} a month of Medicare savings; the payslips deduct none. Set Medicare to N and rerun the proposal.",
    "med_off": "The payslips deduct Medicare; the proposal counts no saving on it, leaving {pay_med_sav} a month out of the promise. Set Medicare to Y and rerun the proposal.",
    "fee_mismatch": "The proposal used a {fee_eng} fee; payroll deducts {fee_pay}. The difference lands in the allotment. Set the proposal fee to the payroll fee and rerun.",
    "salary_zero": "The proposal calculated this employee on zero income while the census carries pay. Check the census salary and the buffer setting, then rerun the proposal.",
    "premium_mismatch": "The payslip premium is not the proposal premium, so every saving sits on the wrong base. Align the premiums and rerun both.",
    "premium_not_pretax": "{premium_pretax_gap} a month of the premium stayed inside federal taxable wages. Payroll setup: take the whole premium pre-tax.",
    "premium_not_medicare": "{medg_gap} a month of the premium stayed inside Medicare wages. Payroll setup: take the whole premium out of Medicare wages.",
    "reimb_missing": "The premium is deducted and never reimbursed, so the employee is paying it. Payroll setup: add the reimbursement line.",
    "reimb_partial": "The reimbursement does not equal the premium; the difference hits take home pay every month. Align them in payroll.",
    "ret_changed": "The retirement deduction moved between the payslips, so the premium's effect is mixed with a retirement recalculation. Ask for a mock that changes only the premium.",
    "fixed_fed": "Payroll withholds the same federal tax before and after, so the premium produces no federal saving for this employee.",
    "fed_zero": "Only {fed_before_m} a month of federal tax existed and the premium wipes it out; the real savings are less than the proposal assumed, and when they are less than the fee, take home falls.",
    "w4_diff": "Payroll and the census hold different W-4 details: {w4_text}. Align them and rerun the proposal.",
    "gross_moved": "Gross pay differs between the two payslips, so more than the premium changed. Ask for a mock that changes only the premium.",
    "identity_fee_stmt": "The payslip figures do not add up: tax saved less the fee does not equal the take home change. Not verified; check the payslips.",
    "identity_fee_prop": "The payslip figures do not add up: tax saved less the fee does not equal the take home change (fee taken from the proposal; the statement prints none). Not verified; check the payslips.",
    "report_internal": "The report's own arithmetic fails: gross savings less fee does not equal the allotment. Regenerate the report.",
    "fed_mismatch": "The report promises {eng_fed_sav} a month of federal saving; payroll saved {pay_fed_sav}. {rerun_fix}",
    "census_ss_not_n": "Neither payslip deducts Social Security, so no saving on it can be promised for this employee. Set the census SocialSec column to N and rerun the proposal.",
    "census_ss_not_y": "The payslips deduct Social Security and the census says N, so the promise leaves the Social Security saving out. Set the census SocialSec column to Y and rerun the proposal.",
    "census_med_not_n": "Neither payslip deducts Medicare. Set the census Medicare column to N and rerun the proposal.",
    "state_mo": "Missouri rounds state tax to whole dollars each pay; small differences are expected.",
    "state_generic": "State tax differs by this amount. Check the state W-4 details on the census.",
    "fica": "Social Security and Medicare differ from the proposal by this amount. Check participation and payroll rounding.",
}


def main():
    columns = ([{"columnId": "in_" + k, "type": "input", "condition": {"inputVariable": k}} for k in IN]
               + [{"columnId": "out_" + k, "type": "output", "columnOutput": {"outputVariable": k}} for k in OUTS])
    refs = {c["outputScalarValue"]["value"] for r in ROWS for c in r["cells"] if c.get("column") == "out_detail"}
    missing = refs - set(DETAILS)
    unused = set(DETAILS) - refs
    if missing or unused:
        raise SystemExit(f'detail templates out of sync: missing {missing}, unused {unused}')
    # Wording bans, enforced at build time so no future edit can reintroduce them.
    banned = ('tax table', 'predates', 'out of date', 'outdated', 'current engine', 'the engine')
    for k, v in DETAILS.items():
        for b in banned:
            if b in v.lower():
                raise SystemExit(f'forbidden wording "{b}" in detail {k}')
    for r in ROWS:
        for c in r['cells']:
            if c.get('column') == 'out_label':
                lab = (c['outputScalarValue']['value'] or '').lower()
                for b in banned:
                    if b in lab:
                        raise SystemExit(f'forbidden wording "{b}" in label of {r["name"]}')
    doc = {"export": {"data": {"rules": [{"ruleAlias": "causeAttribution", "type": "decision-table",
                                          "decisionTable": {"columns": columns, "rows": ROWS}}],
                               "details": DETAILS}}}
    with open(OUT, 'w') as f:
        json.dump(doc, f, indent=1)
    print(f'wrote {OUT}: {len(ROWS)} rows, {len(IN)} inputs, {len(DETAILS)} details')


if __name__ == '__main__':
    main()
