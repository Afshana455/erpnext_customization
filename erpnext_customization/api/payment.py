import requests
import frappe


INTEGRATION_SITE = "http://bank_integration.site:8000"


def get_integration_headers():
    credentials = frappe.get_single("Integration Site Keys")

    api_key = credentials.api_key
    api_secret = credentials.secret_key

    if not api_key or not api_secret:
        frappe.throw("Integration API credentials are not configured.")

    return {
        "Authorization": f"token {api_key}:{api_secret}",
        "Content-Type": "application/json"
    }


def call_integration_api(method, payload):
    url = f"{INTEGRATION_SITE}/api/method/{method}"

    try:
        response = requests.post(
            url,
            json=payload,
            headers=get_integration_headers(),
            timeout=15
        )
    except requests.RequestException:
        frappe.log_error(
            title="Integration Connection Error",
            message=frappe.get_traceback()
        )
        frappe.throw("Unable to connect to bank integration site.")

    if response.status_code != 200:
        frappe.log_error(
            title="Integration API Error",
            message=response.text
        )

        try:
            error_data = response.json()
            error_message = (
                error_data.get("message")
                or error_data.get("exc_type")
                or error_data.get("exception")
                or response.text
            )
        except ValueError:
            error_message = response.text

        frappe.throw(
            f"Bank integration site returned HTTP {response.status_code}: {error_message}"
        )

    try:
        result = response.json()
    except ValueError:
        frappe.log_error(
            title="Invalid Integration Response",
            message=response.text
        )
        frappe.throw("Bank integration site returned an invalid response.")

    data = result.get("message")

    if not data:
        frappe.throw("Bank integration site returned an unexpected response.")

    return data


@frappe.whitelist()
def initiate_bank_payment(invoice, amount, source_account,supplier_account, mode_of_payment):
    if not invoice:
        frappe.throw("Purchase Invoice is required.")

    if amount is None or amount == "":
        frappe.throw("Payment amount is required.")

    if not source_account:
        frappe.throw("Company bank account is required.")
    if not supplier_account:
        frappe.throw("Supplier bank account is required.")

    if not mode_of_payment:
        frappe.throw("Mode of payment is required.")

    try:
        amount = float(amount)
    except (TypeError, ValueError):
        frappe.throw("Payment amount must be a valid number.")

    if amount <= 0:
        frappe.throw("Payment amount must be greater than zero.")

    purchase_invoice = frappe.get_doc("Purchase Invoice", invoice)

    if purchase_invoice.docstatus != 1:
        frappe.throw("Payment can only be initiated for a submitted Purchase Invoice.")

    outstanding_amount = float(purchase_invoice.outstanding_amount or 0)

    if outstanding_amount <= 0:
        frappe.throw("This Purchase Invoice has no outstanding amount.")

    if amount > outstanding_amount:
        frappe.throw(
            f"Payment amount cannot exceed the outstanding amount of "
            f"{purchase_invoice.currency} {outstanding_amount:.2f}."
        )

    company_bank_account = frappe.db.get_value(
        "Bank Account",
        {
            "name": source_account,
            "company": purchase_invoice.company,
            "is_company_account": 1,
            "disabled": 0
        },
        ["name", "bank_account_no", "account_name"],
        as_dict=True
    )

    if not company_bank_account:
        frappe.throw(
            "Selected company bank account is invalid or does not belong to this company."
        )

    if not company_bank_account.bank_account_no:
        frappe.throw("Selected company bank account does not have an account number.")
    
    supplier_bank_account = frappe.db.get_value("Bank Account",{ "name": supplier_account, "party_type": "Supplier", "party": purchase_invoice.supplier, "disabled": 0},[ "name", "bank_account_no", "account_name"], as_dict=True)
    
    if not supplier_bank_account:
        frappe.throw(f"Selected bank account does not belong to supplier "
        f"{purchase_invoice.supplier}.")

    if not supplier_bank_account.bank_account_no:
        frappe.throw("Selected supplier bank account "
         "does not have an account number.")

    request_id = frappe.generate_hash(length=20)

    payload = {
        "request_id": request_id,
        "erp_site": frappe.local.site,
        "erp_doctype": "Purchase Invoice",
        "erp_document_name": purchase_invoice.name,
        "amount": amount,
        "currency": purchase_invoice.currency,
        "source_account": company_bank_account.bank_account_no,
        "beneficiary_account": supplier_bank_account.bank_account_no,
        "mode_of_payment": mode_of_payment
    }

    data = call_integration_api(
        "bank_integration.api.payment.create_payment_request",
        payload
    )

    transaction_log = frappe.get_doc({
        "doctype": "Bank Transaction Log",
        "transaction_id": request_id,
        "transaction_date": frappe.utils.now_datetime(),
        "status": "Initiated",
        "payment_request_id": data.get("request_id"),
        "erp_site": frappe.local.site,
        "erp_doctype": "Purchase Invoice",
        "erp_document": purchase_invoice.name,
        "invoice_amount": purchase_invoice.grand_total,
        "transaction_amount": amount,
        "currency": purchase_invoice.currency,
        "mode_of_payment": mode_of_payment,
        "otp_status": "Pending",
        "bank_response_code": "",
        "bank_response_message": ""
    })

    transaction_log.insert(ignore_permissions=True)

    payment_row = purchase_invoice.append(
        "custom_payment_details",
        {
            "payment_id": data.get("request_id"),
            "amount": amount,
            "currency": purchase_invoice.currency,
            "outsanding_amount": outstanding_amount,
            "payment_status": "OTP Pending",
            "payment_date": frappe.utils.now_datetime()
        }
    )

    purchase_invoice.save(ignore_permissions=True)
    frappe.db.commit()

    return {
        "success": True,
        "request_id": data.get("request_id"),
        "otp": data.get("otp"),
        "otp_expires_at": data.get("otp_expires_at"),
        "payment_status": data.get("payment_status", "Initiated"),
        "transaction_log": transaction_log.name,
        "payment_details_row": payment_row.name
    }


@frappe.whitelist()
def validate_bank_payment_otp(invoice, payment_request, otp, transaction_log):
    if not invoice:
        frappe.throw("Purchase Invoice is required.")

    if not payment_request:
        frappe.throw("Payment request is required.")

    if not otp:
        frappe.throw("OTP is required.")

    if not transaction_log:
        frappe.throw("Transaction log is required.")

    purchase_invoice = frappe.get_doc("Purchase Invoice", invoice)

    if purchase_invoice.docstatus != 1:
        frappe.throw("Purchase Invoice must be submitted.")

    log = frappe.get_doc("Bank Transaction Log", transaction_log)

    if log.erp_document != purchase_invoice.name:
        frappe.throw("Transaction log does not belong to this Purchase Invoice.")

    if log.payment_request_id != payment_request:
        frappe.throw("Payment request does not match the transaction log.")

    payment_row = None
    for row in (purchase_invoice.custom_payment_details or []):
        if row.payment_id == payment_request:
            payment_row = row
            break

    if not payment_row:
        frappe.throw("Payment Details row was not found for this payment request.")

    data = call_integration_api(
        "bank_integration.api.otp.validate_payment_otp",
        {
            "payment_request": payment_request,
            "entered_otp": str(otp)
        }
    )

    otp_status = data.get("otp_status")

    valid_otp_statuses = {
        "Pending",
        "Verified",
        "Invalid",
        "Expired",
        "Failed"
    }

    if otp_status not in valid_otp_statuses:
        frappe.throw(
            f"Unexpected OTP status received from bank integration site: {otp_status}"
        )

    log.otp_status = otp_status

    if otp_status == "Invalid":
        log.status = "Initiated"
        payment_row.payment_status = "OTP Pending"
        log.bank_response_code = ""
        log.bank_response_message = ""

    elif otp_status == "Expired":
        log.status = "Failed"
        payment_row.payment_status = "Failed"
        log.bank_response_code = ""
        log.bank_response_message = ""

    elif otp_status == "Failed":
        log.status = "Failed"
        payment_row.payment_status = "Failed"
        log.bank_response_code = ""
        log.bank_response_message = ""

    elif otp_status == "Pending":
        log.status = "Initiated"
        payment_row.payment_status = "OTP Pending"
        log.bank_response_code = ""
        log.bank_response_message = ""

    elif otp_status == "Verified":
        bank_payment_status = data.get("payment_status") or "Failed"

        valid_payment_statuses = {
            "Initiated",
            "Pending",
            "Completed",
            "Failed",
            "Rejected"
        }

        if bank_payment_status not in valid_payment_statuses:
            frappe.throw(
                f"Unexpected payment status received from bank integration site: {bank_payment_status}"
            )

        payment_status_map = {
            "Initiated": "Payment Initiated",
            "Pending": "Pending",
            "Completed": "Completed",
            "Failed": "Failed",
            "Rejected": "Rejected"
        }

        invoice_payment_status = payment_status_map.get(bank_payment_status, "Error")

        log.status = bank_payment_status
        log.bank_response_code = data.get("response_code") or ""
        log.bank_response_message = data.get("response_message") or ""

        payment_row.payment_status = invoice_payment_status

    log.save(ignore_permissions=True)
    purchase_invoice.save(ignore_permissions=True)
    frappe.db.commit()

    return {
        "success": data.get("success"),
        "request_id": data.get("request_id", payment_request),
        "otp_status": otp_status,
        "payment_status": data.get("payment_status"),
        "response_code": data.get("response_code"),
        "response_message": data.get("response_message"),
        "transaction_id": data.get("transaction_id"),
        "debit_account_number": data.get("debit_account_number"),
        "beneficiary_account_number": data.get("beneficiary_account_number"),
        "amount": data.get("amount"),
        "currency": data.get("currency"),
        "transaction_log": log.name,
        "payment_details_row": payment_row.name
    }