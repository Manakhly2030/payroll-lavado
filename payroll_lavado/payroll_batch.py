import datetime
from datetime import date
from typing import List

import frappe
from frappe.utils import cint, now, time_diff_in_hours, getdate
from frappe.utils.logger import get_logger


# TODO: Add hook on employee & salary structure assignment on save event :
#  create employee chengelog record with the new data
# TODO: Check if the employee transfer updates the employee record; then no need to ad hook into employee transfer too
# TODO: add screen payroll lavado screen
# - user should select the company
# - select start date
# - select end date
class PayrollLavaDo:
    penalty_policy_groups = None
    penalty_policies = None
    shift_types = None

    @staticmethod
    def get_penalty_policy_groups():
        if PayrollLavaDo.penalty_policy_groups:
            PayrollLavaDo.penalty_policy_groups.clear()
        PayrollLavaDo.penalty_policy_groups = frappe.get_all("Lava Penalty Group", order_by='title')
        return

    @staticmethod
    def get_policy_group_by_id(policy_group_id, groups=None):
        if not groups:
            groups = PayrollLavaDo.penalty_policy_groups

        for group in groups:
            if group.name == policy_group_id:
                return group
        return None

    @staticmethod
    def get_shift_type_by_id(shift_type_id, shift_types=None):
        if not shift_types:
            shift_types = PayrollLavaDo.shift_types

        for shift_type in shift_types:
            if shift_type.name == shift_type_id:
                return shift_type_id
        return None

    @staticmethod
    def get_policy_by_id(policy_id, policies=None):
        if not policies:
            policies = PayrollLavaDo.penalty_policies

        for policy in policies:
            if policy.name == policy_id:
                return policy
        return None

    @staticmethod
    def get_policy_by_filters(policies, group_name, subgroup_name, occurrence_number, gap_duration_in_minutes):
        # the policies are being assumed that they sorted by group, subgroup, tolerance_duration desc,
        # occurrence_number desc
        for policy in policies:
            if policy.penalty_group == group_name and \
                    policy.penalty_subgroup == subgroup_name and \
                    policy.occurence_number <= occurrence_number and \
                    policy.duration_tolerance < gap_duration_in_minutes:
                return policy
        return None

    @staticmethod
    def get_penalty_policies(company):
        if PayrollLavaDo.penalty_policies:
            PayrollLavaDo.penalty_policies.clear()
        rows = frappe.db.sql(f"""
                                    SELECT 
                                        p.name, p.title AS policy_title, p.penalty_group, p.occurrence_number,
                                        p.deduction_in_days, p. deduction_amount,
                                        p.penalty_subgroup,
                                        p.tolerance_duration,
                                        d.name AS designation_name,
                                        g.deduction_rule, g.reset_duration
                                    FROM 
                                        `tabLava Penalty Policy` AS p 
                                    INNER JOIN `tabPolicy Designations` AS d
                                        ON p.name = d.parent
                                    INNER JOIN `tabLava Penalty Group` AS g
                                        ON p.penalty_group = g.name
                                    WHERE
                                        p.enabled= 1 AND p.company = '{company}'
                                    ORDER BY p.penalty_group, p.penalty_subgroup, p.tolerance_duration desc,
                                     p.occurrence_number desc
                                """, as_dict=1)
        last_parent_policy = None
        for row in rows:
            if not last_parent_policy or last_parent_policy.name != row.name:
                last_parent_policy = PayrollLavaDo.penalty_policies.append(
                    {'policy_name': row.name,
                     'policy_title': row.policy_title,
                     'penalty_group': row.penalty_group.lower(),
                     'deduction_rule': row.deduction_rule.lower(),
                     'reset_duration': row.reset_duration,
                     'subgroup': row.penalty_subgroup.lower(),
                     'deduction_in_days': row.deduction_in_days,
                     'deduction_amount': row.deduction_amount,
                     'tolerance_duration': row.tolerance_duration,
                     'designations': None})
            else:
                last_parent_policy.designations.append({'designation_name': row.designation_name})

    @staticmethod
    def create_resume_batch(company: str, start_date: date, end_date: date):
        # this is the main entry point of the entire batch, and it can be called from a UI screen on desk,
        # or from a background scheduled job
        PayrollLavaDo.add_action_log("Start batch", "Log")
        batch_id = ""
        last_processed_employee_id = ""
        PayrollLavaDo.get_penalty_policy_groups()
        PayrollLavaDo.get_penalty_policies(company)

        PayrollLavaDo.add_action_log(action="Start validating shift types.")
        shift_types = frappe.get_all("Shift Type", ['name', 'start_time', 'end_time', 'enable_auto_attendance',
                                                    'process_attendance_after',
                                                    'last_sync_of_checkin'])

        try:
            PayrollLavaDo.validate_shift_types(shift_types)
            PayrollLavaDo.shift_types = shift_types
        except:
            PayrollLavaDo.add_action_log(
                action="Due to the batches validation issue, exit the Batch Process for Company: {}, start date: {}, "
                       "end date: {}".format(company, start_date, end_date))
            return

        PayrollLavaDo.create_employees_first_changelog_records(company)
        PayrollLavaDo.add_action_log(
            action="Created the first employees changelog records if missing, Batch Process for Company: {}, "
                   "start date: {}, "
                   "end date: {}".format(company, start_date, end_date))

        PayrollLavaDo.add_action_log(
            action="Decide to resume or create a new Batch Process for Company: {}, start date: {},"
                   "end date: {}".format(company, start_date, end_date))
        progress_batches = frappe.get_all("Lava Payroll LavaDo", {"status": "In Progress", "company": company},
                                          ['start_date', 'end_date'])
        if len(progress_batches) > 1:
            exp_msg = frappe._("Company {} has more than a batch in progress, Please check".format(company))
            get_logger(exp_msg)
            frappe.throw(exp_msg)

        elif len(progress_batches) == 1:
            if progress_batches[0].start_date != start_date or progress_batches[0].end_date != end_date:
                exp_msg = frappe._(
                    "Company {} has  batch {} in progress but with different date range than requested,"
                    " Please check".format(
                        company, progress_batches[0].name))
                get_logger(exp_msg)
                frappe.throw(exp_msg)
        else:
            batch_id = progress_batches[0].name
            last_processed_employee_id = PayrollLavaDo.get_batch_last_processed_employee_id(batch_id)
            PayrollLavaDo.add_action_log(action="Resume batch {} for company: {}".format(batch_id, company))
            if last_processed_employee_id:
                PayrollLavaDo.delete_last_processed_employee_batch_records(last_processed_employee_id, batch_id)
                PayrollLavaDo.add_action_log(
                    action="Batch: {} for Company: {} removed the records of"
                           " employee {}".format(batch_id, company,
                                                 last_processed_employee_id))

        if batch_id == "":
            new_batch = frappe.new_doc("Lava Payroll LavaDo")
            new_batch.company = company
            new_batch.start_date = frappe.utils.today()
            new_batch.status = "In Progress"
            new_batch.save(ignore_permissions=True)
            batch_id = new_batch.name
            PayrollLavaDo.add_action_log(
                action="Batch: {} for Company: {} created".format(batch_id, company))
        PayrollLavaDo.add_action_log(
            action="Batch: {} starting auto attendance".format(batch_id))
        for shift_type in PayrollLavaDo.shift_types:
            if shift_type.enable_auto_attendance:
                PayrollLavaDo.run_standard_auto_attendance(shift_types)

        PayrollLavaDo.process_employees(batch_id, company, start_date, end_date, last_processed_employee_id)
        PayrollLavaDo.add_action_log(
            action="Batch: {} completed and will update the status".format(batch_id))
        PayrollLavaDo.update_batch_status(batch_id, status="Completed")

    @staticmethod
    def add_action_log(action: str, action_type: str = "LOG", notes: str = None):
        print(now(), action, action_type)
        new_doc = frappe.new_doc("Lava Action Log")
        new_doc.action = action
        new_doc.action_type = action_type
        new_doc.notes = notes
        new_doc.save(ignore_permissions=True)

    @staticmethod
    def get_batch_last_processed_employee_id(batch_id):
        try:
            employee_id = frappe.get_last_doc('Lava Batch Object', {"batch_id": batch_id, "object_type": 'Employee'},
                                              ['object_id'])
        except frappe.DoesNotExistError:
            employee_id = None

        return employee_id

    @staticmethod
    def delete_last_processed_employee_batch_records(employee: str, batch_id: str):
        batch_related_doctypes = ["TimeSheet", "Lava Penalty Record", "Additional Salary"]
        employee_batch_records_ids = frappe.get_all("Lava Batch Object",
                                                    {'object_type': ['in', batch_related_doctypes],
                                                     'object_id': employee,
                                                     'batch_id': batch_id},
                                                    ['object_type', 'object_id'])

        for record in employee_batch_records_ids:
            frappe.delete_doc(doctype=record.object_type, name=record.object_id, force=1)

    @staticmethod
    def validate_shift_types(shift_types):
        # FIXME: ask about the difference between "Last Sync of Checkin" and "Process Attendance After" in the shift type
        invalid_shift_types = []
        for shift_type in shift_types:
            if (not shift_type.enable_auto_attendance
                    or not shift_type.process_attendance_after
                    or not shift_type.last_sync_of_checkin
            ):
                invalid_shift_types.append(shift_type.name)

        if invalid_shift_types:
            exp_msg = "Shift types {} are missing data".format(invalid_shift_types)
            frappe.throw(frappe._(exp_msg))
            get_logger(exp_msg)

    @staticmethod
    def process_employees(batch_id: str, company: str, start_date: date, end_date: date,
                          last_processed_employee_id: str = None):
        employees = PayrollLavaDo.get_company_employees(company, batch_id, last_processed_employee_id)
        for employee in employees:
            PayrollLavaDo.create_batch_object_record(batch_id=batch_id, object_type="Employee",
                                                     object_id=employee.employee_id, status="In progress", notes="")
            PayrollLavaDo.add_action_log(
                action="Start process employee: {} for company: {} into batch : {}".format(employee.employee_id,
                                                                                           company,
                                                                                           batch_id))

            attendance_list = PayrollLavaDo.get_attendance_list(employee.employee_id, start_date, end_date)
            employee_changelog_records = PayrollLavaDo.get_employee_changelog_records(max_date=end_date,
                                                                                      employee_id=employee.employee_id)
            employee_timesheet = PayrollLavaDo.create_employee_timesheet(employee_id=employee.name,
                                                                         company=company
                                                                         )

            for attendance in attendance_list:
                employee_changelog_record = PayrollLavaDo.get_employee_changelog_record(
                    attendance_date=attendance.attendance_date,
                    employee_change_log_records=employee_changelog_records)
                # TODO: handle the exceptions
                PayrollLavaDo.calc_attendance_working_hours_breakdowns(attendance, employee_changelog_record)
                PayrollLavaDo.add_timesheet_record(employee_timesheet, attendance)
                employee_applied_policies = PayrollLavaDo.get_employee_applied_policies(
                    employee_changelog_record['designation'])
                PayrollLavaDo.add_penalties(employee_changelog_record, batch_id, attendance, employee_applied_policies)

                employee_timesheet.save(ignore_permssions=True)
                PayrollLavaDo.create_batch_object_record(batch_id=batch_id, object_type="Timesheet",
                                                         object_id=employee_timesheet.name,
                                                         status="In progress", notes="")

                PayrollLavaDo.add_action_log(
                    action="End process employee: {} for company: {} into batch : {}".format(employee.name, company,
                                                                                             batch_id))

    @staticmethod
    def add_penalties(employee_changelog_record, batch_id, attendance, applied_policies):
        existing_penalty_records = frappe.get_all("Lava Penalty Record",
                                                  filters={"employee": employee_changelog_record.employee,
                                                           "penalty_date": ['=', attendance.attendance_date]},
                                                  order_by='modified')

        for existing_penalty_record in existing_penalty_records:
            policy = PayrollLavaDo.get_policy_by_id(existing_penalty_record.penalty_policy, policies=applied_policies)
            PayrollLavaDo.get_policy_and_add_penalty_record(
                employee_changelog_record=employee_changelog_record,
                attendance=attendance,
                policy_group_obj=PayrollLavaDo.get_policy_group_by_id(policy.penalty_group),
                policy_subgroup=policy.penalty_subgroup,
                batch_id=batch_id,
                applied_policies=applied_policies)

        if attendance.status.lower() == "absent":
            PayrollLavaDo.get_policy_and_add_penalty_record(
                employee_changelog_record=employee_changelog_record,
                attendance=attendance,
                policy_group_obj=PayrollLavaDo.get_policy_group_by_id("attendance"),
                policy_subgroup="attendance absence",
                batch_id=batch_id,
                applied_policies=applied_policies)
        if attendance.late_entry:
            PayrollLavaDo.get_policy_and_add_penalty_record(
                employee_changelog_record=employee_changelog_record,
                attendance=attendance,
                policy_group_obj=PayrollLavaDo.get_policy_group_by_id("attendance"),
                policy_subgroup="attendance check-in",
                batch_id=batch_id,
                applied_policies=applied_policies)
        if attendance.early_exit:
            PayrollLavaDo.get_policy_and_add_penalty_record(
                employee_changelog_record=employee_changelog_record,
                attendance=attendance,
                policy_group_obj=PayrollLavaDo.get_policy_group_by_id("attendance"),
                policy_subgroup="attendance check-out",
                batch_id=batch_id,
                applied_policies=applied_policies)

    @staticmethod
    def get_policy_and_add_penalty_record(employee_changelog_record, attendance,
                                          policy_group_obj, policy_subgroup, batch_id, applied_policies):
        gap_duration_in_minutes = 0
        if policy_subgroup.lower() == "attendance check-in":
            gap_duration_in_minutes = attendance.lava_entry_duration_difference
        elif policy_subgroup.lower() == "attendance check-out":
            gap_duration_in_minutes = attendance.lava_exit_duration_difference
        policy_occurrence_number = PayrollLavaDo.get_penalty_records_number_within_duration(
            employee=employee_changelog_record.employee,
            check_date=attendance.attendance_date,
            duration_in_days=policy_group_obj.reset_duration,
            policy_subgroup=policy_subgroup) + 1
        occurred_policy = PayrollLavaDo.get_policy_by_filters(
            policies=applied_policies,
            group_name=policy_group_obj.name,
            subgroup_name=policy_subgroup,
            occurrence_number=policy_occurrence_number,
            gap_duration_in_minutes=gap_duration_in_minutes)
        if occurred_policy:
            PayrollLavaDo.add_penalty_record(employee_changelog_record=employee_changelog_record,
                                             batch_id=batch_id,
                                             attendance=attendance,
                                             policy=occurred_policy,
                                             policy_occurrence_number=policy_occurrence_number,
                                             existing_penalty_record=None)

    @staticmethod
    def get_penalty_records_number_within_duration(employee, check_date, duration_in_days, policy_subgroup):
        # TODO: consider the correction records
        from_date = datetime.datetime.strptime(check_date, "%m/%d/%y") - datetime.timedelta(days=duration_in_days)
        penalty_records_number_within_duration = frappe.get_all("Lava Penalty Record",
                                                                filters={
                                                                    "employee": employee,
                                                                    "penalty_date": ['>=', from_date],
                                                                    "policy_subgroup": policy_subgroup
                                                                },
                                                                order_by='penalty_date')
        return len(penalty_records_number_within_duration)

    @staticmethod
    def get_employee_applied_policies(employee_designation):
        applied_policies = []
        for policy in PayrollLavaDo.penalty_policies:
            for designation in policy.designations:
                if designation.name == employee_designation:
                    applied_policies.append(policy)
                    break
        return applied_policies

    @staticmethod
    def get_attendance_list(employee: str, start_date: date, end_date: date):
        return frappe.get_all("Attendance",
                              {'employee': employee, 'attendance_date': ['between', [start_date, end_date]]},
                              ['*'])

    @staticmethod
    def create_employees_first_changelog_records(company: str):
        # TODO: future enhancement: We can change the employee transfer doctype to enhance the result accuracy
        rows = frappe.db.sql(f"""
                                          SELECT 
                                              e.name AS employee_id,e.designation, e.company As employee_company,
                                              ssa.name AS salary_structure_assignment,
                                               ssa.from_date AS salary_structure_assignment_from_date,
                                              e.modified AS last_modified_date,
                                               e.default_shift AS employee_default_shift,
                                              sha.shift_type AS shift_assignment_shift_type
                                          FROM 
                                              `tabEmployee` AS e INNER JOIN `tabSalary Structure Assignment` ssa
                                              ON e.name = ssa.employee and ssa.company = '{company}'
                                          LEFT JOIN `tabShift Assignment` AS sha
                                                ON sha.status = 'Active' 
                                                AND sha.employee = e.name 
                                                AND sha.company = e.company
                                                AND CURDATE() BETWEEN sha.start_date, sha.end_date
                                            INNER JOIN `tabSalary Structure` AS ss
                                                ON sha.salary_structure = ss.name AND ss.company = ='{company}'
                                          WHERE
                                              e.company = '{company}'
                                              AND e.name NOT IN
                                              (
                                                SELECT employee
                                                from `tabLava Employee Payroll Changelog`
                                                WHERE company = '{company}'
                                              )
                                      """, as_dict=1)
        for row in rows:
            if row.designation is None or row.salary_structure_assignment is None:
                exp_msg = "Employee {} doesn't have designation and/or salary " \
                          "structure assignment".format(row.employee_id)
                frappe.throw(frappe._(exp_msg))
                get_logger(exp_msg)
            else:
                employee_change_log_record = frappe.new_doc('Lava Employee Payroll Changelog')
                employee_change_log_record.employee = row.employee_id
                employee_change_log_record.company = row.employee_company
                employee_change_log_record.shift_type = row.shift_assignment_shift_type or row.employee_default_shift
                employee_change_log_record.change_date = row.salary_structure_assignment_from_date
                employee_change_log_record.designation = row.designation
                employee_change_log_record.attendance_plan = row.attendance_plan
                employee_change_log_record.salary_structure_assignment = row.salary_structure_assignment
                employee_change_log_record.save(ignore_permissions=True)

    @staticmethod
    def get_employee_changelog_records(max_date: date, employee_id: str):
        employee_changelogs = frappe.get_all("Lava Employee Payroll Changelog",
                                             filters={'employee': employee_id, 'change_date': ['<=', max_date]},
                                             order_by="change_date desc", fields=['*'])
        return employee_changelogs

    @staticmethod
    def get_employee_changelog_record(attendance_date: date, employee_change_log_records):
        for record in employee_change_log_records:
            if record['change_date'] <= attendance_date:
                return record
        return None

    @staticmethod
    def get_company_employees(company: str, batch_id: str, last_processed_employee_id: str = None):
        employee_list = frappe.db.sql(f""" 
                        SELECT 
                            name AS employee_id 
                        FROM 
                            `tabEmployee` 
                        WHERE company= '{company}'
                        AND employee.name NOT IN 
                            (SELECT object_id
                            FROM `tabBatch Object` 
                            WHERE  object_type = "Employee" 
                                AND employee.batch_id = {batch_id}
                                AND employee_name != {last_processed_employee_id})""", as_dict=1)

        return employee_list

    @staticmethod
    def create_batch_object_record(batch_id, object_type, object_id, status=None, notes=None):
        batch_object_record = frappe.new_doc("Lava Batch Object")
        batch_object_record.batch_id = batch_id
        batch_object_record.object_type = object_type
        batch_object_record.object_id = object_id
        batch_object_record.status = status
        batch_object_record.notes = notes
        batch_object_record.save(ignore_permissions=True)

    @staticmethod
    def run_standard_auto_attendance(shift_types: List[dict]):
        for shift_type in shift_types:
            shift_doc = frappe.get_doc('Shift Type', shift_type.name)
            shift_doc.process_auto_attendance()
            frappe.db.commit()

    @staticmethod
    def create_employee_timesheet(employee_id, company):
        timesheet = frappe.new_doc('Timesheet')
        # TODO:Future enhancement avoid duplications in timesheet creation
        timesheet.employee = employee_id
        timesheet.company = company
        return timesheet

    @staticmethod
    def calc_attendance_working_hours_breakdowns(attendance, employee_changelog_record):
        shift_type = PayrollLavaDo.get_shift_type_by_id(attendance.shift)
        if attendance.late_entry:
            attendance.lava_entry_duration_difference = time_diff_in_hours(attendance.in_time.time(),
                                                                           shift_type.start_time)
        if attendance.early_exit:
            attendance.lava_exit_duration_difference = time_diff_in_hours(shift_type.end_time,
                                                                          attendance.out_time.time())
        if shift_type.end_time.time() > shift_type.start_time.time():
            attendance.lava_planned_working_hours = time_diff_in_hours(shift_type.end_time, shift_type.start_time)
        else:
            attendance.lava_planned_working_hours = time_diff_in_hours(shift_type.start_time,
                                                                       datetime.datetime.strptime("23:59:00",
                                                                                                  "%H:%M:%S")) + (
                                                            1 / 60)
            attendance.lava_planned_working_hours += time_diff_in_hours(
                datetime.datetime.strptime("00:00:00", "%H:%M:%S"),
                shift_type.end_time)
        attendance.save(ignore_permissions=True)

    @staticmethod
    def add_timesheet_record(employee_timesheet, attendance, activity_type=None):
        employee_timesheet.append("time_logs", {
            "activity_type": activity_type,
            "hours": attendance.working_hours,
            "from_time": attendance.in_time
        })

    @staticmethod
    def add_penalty_record(employee_changelog_record, batch_id,
                           attendance, policy,
                           policy_occurrence_number,
                           existing_penalty_record):

        penalty_record = existing_penalty_record
        if not penalty_record:
            penalty_record = frappe.new_doc("Lava Penalty Record")

        deduction_in_days_amount = employee_changelog_record.hourly_rate * policy.deduction_in_days
        deduction_absolute_amount = policy.absolute_amount
        applied_penalty_deduction_amount = 0

        if policy.deduction_rule == "biggest":
            if deduction_absolute_amount > deduction_in_days_amount:
                applied_penalty_deduction_amount = deduction_absolute_amount
            else:
                applied_penalty_deduction_amount = deduction_in_days_amount
        elif policy.deduction_rule == "smallest":
            if deduction_absolute_amount < deduction_in_days_amount:
                applied_penalty_deduction_amount = deduction_absolute_amount
            else:
                applied_penalty_deduction_amount = deduction_in_days_amount
        elif policy.deduction_rule == "absolute amount":
            applied_penalty_deduction_amount = deduction_absolute_amount
        elif policy.deduction_rule == "deduction in days":
            applied_penalty_deduction_amount = deduction_in_days_amount
        else:
            applied_penalty_deduction_amount = 0
            exp_msg = "Unknown deduction rule '{}' for attendance date {} for employee {}".format(
                policy.deduction_rule,
                attendance.attendance_date,
                employee_changelog_record.employee
            )
            get_logger(exp_msg)
            return

        penalty_record.employee = employee_changelog_record.employee
        penalty_record.penalty_policy = policy.name
        penalty_record.penalty_date = attendance.attendance_date
        penalty_record.occurrence_number = policy_occurrence_number
        penalty_record.penalty_amount = applied_penalty_deduction_amount
        penalty_record.action_type = "Automatic"
        penalty_record.notes = ""
        penalty_record.lava_payroll_batch = batch_id

        penalty_record.save(ignore_permissions=True)

        PayrollLavaDo.create_batch_object_record(batch_id=batch_id, object_type="Lava Penalty Record",
                                                 object_id=penalty_record.name,
                                                 status="Created", notes="")
        if applied_penalty_deduction_amount > 0:
            PayrollLavaDo.add_additional_salary(penalty_record, batch_id)

    @staticmethod
    def add_additional_salary(penalty_record, batch_id):
        additional_salary_record = frappe.new_doc("Additional Salary")
        additional_salary_record.employee = penalty_record.employee
        additional_salary_record.payroll_date = penalty_record.penalty_date
        additional_salary_record.salary_component = "HR Policy Deduction"  # TODO: Add by patch
        additional_salary_record.overwrite_salary_structure_amount = 0
        additional_salary_record.amount = penalty_record.penalty_amount
        additional_salary_record.reason = f"Apply policy {penalty_record.penalty_policy}, occurrence number {penalty_record.occurrence_number}."
        additional_salary_record.save(ignore_permissions=True)
        PayrollLavaDo.create_batch_object_record(batch_id=batch_id, object_type="Additional Salary",
                                                 object_id=additional_salary_record.name,
                                                 status="Created", notes="")

    @staticmethod
    def update_batch_status(batch_id: str, status: str):
        frappe.set_value("Lava Payroll LavaDo", batch_id, "status", status)


@frappe.whitelist()
def run_payroll_lavado_batch(company=None, start_date=None, end_date=None):
    company = "اكسبنشن خالد السرجاني"
    start_date = getdate("01-10-2022")
    end_date = getdate("31-10-2022")
    PayrollLavaDo.create_resume_batch(company=company
                                      , start_date=start_date,
                                      end_date=end_date)
