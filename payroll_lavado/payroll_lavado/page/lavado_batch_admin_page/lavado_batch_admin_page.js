
frappe.pages['lavado_batch_admin_page'].on_page_load = function (wrapper) {
	new MyPage(wrapper);
}

MyPage = Class.extend({
	init: function (wrapper) {
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: 'Payroll LavaDo Batch Manager',
			single_column: false
		});
		this.make();
	},
	make: function () {
		$(frappe.render_template("lavado_batch_admin_page", this)).appendTo(this.page.main);
        $(document).ready(function(){
             frappe.db.get_list('Company', {
                    fields: ['name']
                }).then(records => {
                    for (var counter in records){
                        let option = new Option(records[counter]['name'], records[counter]['name']);
                        $("#select-company").append(option);
                    }
                });
            });
        $('#select-company').change(function(){
            get_batches();
        });
        $('#btn-refresh').click(function(){
            get_batches();
        });
		$('#btn-run-batch').click(function(){
		    let batch_company = $("#select-company :selected").text();
		    let batch_start_date = new Date($('#batch-start-date').val());
		    let batch_end_date = new Date($('#batch-end-date').val());
		    let chk_clear_error_log_records =(($("#chk-clear-error-log-records").is(":checked"))? 1 : 0);
		    let chk_clear_action_log_records = (($("#chk-clear-action-log-records").is(":checked"))? 1 : 0);
		    let chk_biometric_process = (($("#chk-biometric-process").is(":checked"))? 1 : 0);
		    let chk_batch_objects = (($("#chk-batch-objects").is(":checked"))? 1 : 0);
		    let error_msg = "";
		    if (isNaN(batch_end_date) || isNaN(batch_start_date)){
		        error_msg += ", select dates";
		    }
		    if (batch_end_date <= batch_start_date){
		        error_msg += ", end date must be > start date";
		    }
		    if (error_msg.length >0){
		        frappe.msgprint(__("error message: " + error_msg));
		        return;
		    }
		    let doc_data={
		        "company": batch_company,
		        "start_date": batch_start_date,
		        "end_date": batch_end_date,
		        "chk-clear-error-log-records": chk_clear_error_log_records,
		        "chk-clear-action-log-records": chk_clear_action_log_records,
		        "chk-batch-objects": chk_batch_objects,
		        "chk-biometric-process": chk_biometric_process
		    }
    		run_batch(doc_data);
		 });
	}
})


function run_batch(doc_data) {
	frappe.call({
		method:
			"payroll_lavado.payroll_batch.run_payroll_lavado_batch",
		args: {
			doc: doc_data,
		},
		callback: function (r) {
		if (r.message == "Success") {
            frappe.msgprint(__("Ran batch"));
		} else {
            frappe.throw(__(r.message));
		}
		}
	})
}

function get_batches(){
    let batch_company = $("#select-company :selected").text();
    doc_data = {"company": batch_company};
    frappe.call({
            method:
                "payroll_lavado.payroll_lavado.page.lavado_batch_admin_page.lavado_batch_admin_page.get_payroll_lavado_batches",
            args: {
                filters: doc_data,
            },
            callback: function (r) {
            if (r.message.message == "Success") {
                if(r.message.result){
                    render_batches_data(r.message.result);
                }
            } else {
                frappe.throw(__(r.message.message));
            }
            }
        })
}

function render_batches_data(records){

    if ($("#tbody-batches").length ){
        if ($("#tbody-batches").rows)
            for (let i=0;i< $("#tbody-batches").rows.length ;i++){
                $('#tbody-batches').deleteRow(i);
            }
    }
    if (records.length == 0){
        return;
    }
    if (!$("#tr-head").length){
        let first_record = records[0];
        $("#table-batches").append('<thead>').append("<tr id='tr-head'>");

        for(let key of Object.keys(first_record)){
            $("#tr-head").append(`<th class="col grid-static-col col-xs-4 ">${key}</th>`);
        }
        $("#table-batches").append('<tbody id="tbody-batches">');
    }
    rowIndex = 0;
    for (var record of records){
       console.log(record)
       if ($(`#tr${rowIndex}`).length)
       {
            $(`#tr${rowIndex}`).remove();
       }
       $('#tbody-batches').append(`<tr id=tr${rowIndex}>`);
       $(`#tr${rowIndex}`).append(`<td>${record.batch_id}</td>`);
       $(`#tr${rowIndex}`).append(`<td>${record.company}</td>`);
       $(`#tr${rowIndex}`).append(`<td>${record.start_date}</td>`);
       $(`#tr${rowIndex}`).append(`<td>${record.end_Date}</td>`);       //TODO: fix field name
       $(`#tr${rowIndex}`).append(`<td>${record.status}</td>`);
       $(`#tr${rowIndex}`).append(`<td>${record.batch_process_start_time}</td>`);
       $(`#tr${rowIndex}`).append(`<td>${record.batch_process_end_time}</td>`);
       rowIndex += 1;
    }
}