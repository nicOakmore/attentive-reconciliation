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

IN = ['ret_named_known', 'ret_missing_abs', 'ret_unnamed_abs', 'ret_unnamed_signed', 'ret_zero', 'ti_gap', 'wrong_col_match',
      'slips_present', 'ss_on_slips', 'eng_ss', 'eng_ss_known', 'pay_ss_sav', 'fee_known', 'fee_gap_abs',
      'eng_ti_known', 'eng_ti_zero', 'census_gross', 'premium_known', 'premium_gap_abs', 'fixed_fed',
      'fed_zero', 'w4_diff', 'gross_moved_abs', 'identity_broken', 'identity_explained', 'fee_from_stmt',
      'fed_in_doubt', 'federal_gap_abs', 'state_gap_abs', 'state_is_mo', 'fica_gap_abs', 'ss_mismatch',
      # payroll execution and report integrity
      'pretax_known', 'premium_pretax_gap_abs', 'medg_known', 'medg_gap_abs', 'reimb_missing_sig',
      'reimb_known', 'reimb_gap_abs', 'med_read', 'med_on_slips', 'eng_med', 'eng_med_known',
      'pay_med_sav', 'med_mismatch', 'ret_change_abs', 'report_int_known', 'report_int_gap_abs',
      'census_found', 'census_ss', 'census_med',
      'fed_residual_abs', 'fica_residual_abs', 'state_residual_abs', 'promise_over_ceiling',
      'premium_stayed_taxable_abs', 'taxable_fell_extra_abs', 'premium_stayed_medicare_abs',
      'medicare_fell_extra_abs', 'reimb_short_abs', 'reimb_over_abs']
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
        {"ret_named_known": Y, "ret_missing_abs": gt(0.02), "ret_unnamed_signed": gt(0.02)},
        "Retirement deduction missing from the census", "{ret_missing}", "ret_named_plus"),
    row("the census carries part of the pre-tax deduction payroll takes",
        {"ret_named_known": Y, "ret_missing_abs": gt(0.02), "ret_unnamed_signed": lt(-0.02)},
        "Retirement deduction missing from the census", "{ret_missing}", "ret_part_carried"),
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
    row("part of the premium stayed in federal taxable wages",
        {"pretax_known": Y, "premium_stayed_taxable_abs": gt(1.0)},
        "The premium was not taken pre-tax in full", "{premium_stayed_taxable}", "premium_not_pretax"),
    row("federal taxable wages fell by more than the premium",
        {"pretax_known": Y, "taxable_fell_extra_abs": gt(1.0)},
        "Taxable wages fell by more than the premium", "{taxable_fell_extra}", "taxable_fell_extra"),
    row("part of the premium stayed in Medicare wages",
        {"medg_known": Y, "premium_stayed_medicare_abs": gt(1.0)},
        "The premium did not come out of Medicare wages in full", "{premium_stayed_medicare}", "premium_not_medicare"),
    row("Medicare wages fell by more than the premium",
        {"medg_known": Y, "medicare_fell_extra_abs": gt(1.0)},
        "Medicare wages fell by more than the premium", "{medicare_fell_extra}", "medicare_fell_extra"),
    row("the premium is deducted but never reimbursed",
        {"reimb_missing_sig": Y},
        "The premium is deducted but never reimbursed", "0 - {premium_m}", "reimb_missing"),
    row("the reimbursement is short of the premium",
        {"reimb_known": Y, "premium_known": Y, "reimb_short_abs": gt(1.0)},
        "The reimbursement does not return the whole premium", "{reimb_short}", "reimb_partial"),
    row("the reimbursement exceeds the premium",
        {"reimb_known": Y, "premium_known": Y, "reimb_over_abs": gt(1.0)},
        "The reimbursement returns more than the premium", "{reimb_over}", "reimb_over"),
    row("the retirement deduction changed with the premium",
        {"ret_change_abs": gt(1.0)},
        "The retirement deduction changed with the premium", "{ret_change}", "ret_changed"),
    row("payroll withholds a fixed federal amount",
        {"fixed_fed": Y},
        "Payroll withholds a fixed federal amount", "{fed_before_m}", "fixed_fed"),
    row("the promise exceeds the federal tax available",
        {"promise_over_ceiling": Y},
        "The promised federal saving is more than the federal tax available", "{fed_before_m}", "fed_zero"),
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
    row("federal saving differs by more than the known defects can explain",
        {"fed_in_doubt": N, "fed_residual_abs": gt(1.0)},
        "The proposal's federal saving does not match payroll", "{fed_residual}", "fed_mismatch"),
    # ---- residual component differences -------------------------------------------------
    row("state withholding differs by more than the known defects can explain",
        {"state_residual_abs": gt(1.0)},
        "State withholding does not match the proposal", "{state_residual}", "state_generic"),
    row("Social Security and Medicare differ by more than the known defects can explain",
        {"fica_residual_abs": gt(1.0), "ss_mismatch": N, "med_mismatch": N},
        "Social Security and Medicare withheld do not match the proposal", "{fica_residual}", "fica"),
]

DETAILS = {
    "ret_named_exact": "Payroll takes {ret_missing} a month pre-tax for retirement; the census does not carry it, so the proposal taxed income payroll does not tax." + RET_TAIL,
    "ret_part_carried": "The census is short {ret_missing} a month of the pre-tax deduction payroll takes; the payslip names {ret_named} a month as retirement." + RET_TAIL,
    "ret_named_plus": "Payroll reduces federal wages by {ret_missing} a month more than the census carries; the payslip names {ret_named} as retirement and does not name the other {ret_unnamed}." + RET_TAIL,
    "ret_unnamed": "Payroll takes a pre-tax deduction of {ret_missing} a month that the payslip does not name and the census does not carry." + RET_TAIL,
    "pretax_missing": "The proposal starts from {ti_gap} a month more income than the payslip taxes. Add that amount to the census Other pre-tax column (field Q) and rerun the proposal.",
    "wrong_column": "The proposal starts {ti_gap_abs} a month low: a retirement amount sits in the Other pre-tax column, which also cuts Social Security and Medicare wages. Move it to the 401-k/IRA column and rerun the proposal.",
    "ti_neg": "The proposal starts from {ti_gap_abs} a month less income than the payslip taxes, so a census pre-tax figure is too high. Reduce field Q or field R by that amount and rerun the proposal.",
    "census_missing": "No census row carries this employee's payroll number or name. Add the employee to the census and rerun the proposal.",
    "ss_on": "The proposal counts {eng_ss} a month of Social Security savings; the payslips deduct none. Set SocialSec to N and rerun the proposal.",
    "ss_off": "The payslips deduct Social Security; the proposal counts no saving on it, leaving {pay_ss_sav} a month out of the promise. Set SocialSec to Y and rerun the proposal.",
    "med_on": "The proposal counts {eng_med} a month of Medicare savings; the payslips deduct none. Set Medicare to N and rerun the proposal.",
    "med_off": "The payslips deduct Medicare; the proposal counts no saving on it, leaving {pay_med_sav} a month out of the promise. Set Medicare to Y and rerun the proposal.",
    "fee_mismatch": "The proposal used a {fee_eng} monthly fee; payroll deducts {fee_pay}. Set the EE fee in the proposal program settings to {fee_pay} and rerun the proposal.",
    "salary_zero": "The proposal calculated this employee on zero income while the census carries {census_gross} of annual pay. Set the buffer in the proposal program settings to 100 and rerun the proposal.",
    "premium_mismatch": "The payslip deducts a premium of {premium_m} a month and the proposal used a different figure, so every saving sits on the wrong base. Set the proposal premium to {premium_m} and rerun the proposal.",
    "premium_not_pretax": "Of the {premium_m} premium, {premium_stayed_taxable} a month stayed inside federal taxable wages. Payroll must take the whole premium pre-tax.",
    "premium_not_medicare": "Of the {premium_m} premium, {premium_stayed_medicare} a month stayed inside Medicare wages. Payroll must take the whole premium out of Medicare wages.",
    "reimb_missing": "The premium of {premium_m} a month is deducted and never reimbursed, so the employee pays it. Payroll must add the reimbursement line.",
    "reimb_partial": "The reimbursement is {reimb_short} a month below the {premium_m} premium. Payroll must set the reimbursement to {premium_m} a month.",
    "ret_changed": "The retirement deduction changed by {ret_change_size} a month between the two payslips, so the premium's own effect cannot be isolated. Ask payroll for a mock in which only the premium changes.",
    "fixed_fed": "Payroll withheld {fed_before_m} a month of federal tax on both payslips. Ask payroll why withholding did not move; until it does, no federal saving can be promised to this employee.",
    "fed_zero": "Federal tax withheld is {fed_before_m} a month, so the federal saving cannot exceed {fed_before_m}. The report promises {eng_fed_sav}. Reduce the promised federal saving to {fed_before_m} a month.",
    "w4_diff": "Payroll and the census hold different W-4 details: {w4_text}. Correct the census W-4 columns to match payroll and rerun the proposal.",
    "gross_moved": "Gross pay changed by {gross_moved} a month between the two payslips, so more than the premium changed. Ask payroll for a mock in which only the premium changes.",
    "identity_fee_stmt": "The payslip figures are out by {identity_gap} a month: tax saved less the fee does not equal the take home change. This employee is not verified. Ask payroll for a clean copy of both payslips.",
    "identity_fee_prop": "The payslip figures are out by {identity_gap} a month, with the fee taken from the proposal because the payslip prints none. This employee is not verified. Ask payroll for a payslip showing the fee line.",
    "report_internal": "The report's own arithmetic is out by {report_int_gap} a month: gross savings less the fee does not equal the allotment it promises. Regenerate the proposal report before reconciling it.",
    "fed_mismatch": "After the corrections above, {fed_residual_abs} a month of the federal difference is still unaccounted for. Compare the W-4 details and additional federal withholding on the census against the payslip; if they match, payroll must account for the difference.",
    "census_ss_not_n": "Neither payslip deducts Social Security, so no saving on it can be promised for this employee. Set the census SocialSec column to N and rerun the proposal.",
    "census_ss_not_y": "The payslips deduct Social Security and the census says N, so the promise leaves the Social Security saving out. Set the census SocialSec column to Y and rerun the proposal.",
    "census_med_not_n": "Neither payslip deducts Medicare. Set the census Medicare column to N for this employee and rerun the proposal.",
    "state_generic": "State withholding differs from the proposal by {state_residual_abs} a month beyond what the corrections above explain. Correct the census state marital status and withholding dependents if they do not match the payslip; if they match, payroll must account for the difference.",
    "fica": "Social Security and Medicare withheld differ from the proposal by {fica_residual_abs} a month beyond what the corrections above explain. Correct the census pay frequency if it does not match the payslip; if it matches, payroll must account for the difference.",
    "taxable_fell_extra": "Federal taxable wages fell {taxable_fell_extra} a month more than the {premium_m} premium, so something besides the premium reduced them. Ask payroll for a mock in which only the premium changes.",
    "medicare_fell_extra": "Medicare wages fell {medicare_fell_extra} a month more than the {premium_m} premium, so something besides the premium reduced them. Ask payroll for a mock in which only the premium changes.",
    "reimb_over": "The reimbursement is {reimb_over} a month above the {premium_m} premium. Payroll must set the reimbursement to {premium_m} a month.",
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
