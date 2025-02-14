import requests
import boto3
import pymysql.cursors
import time
from botocore.exceptions import ClientError
from botocore.exceptions import ClientError
from datetime import datetime, timedelta
from decimal import Decimal
from extensions import mail
from flask import Flask, jsonify, make_response, redirect, render_template, request
from flask_awscognito import AWSCognitoAuthentication
from functools import wraps
from lib.data.archive_item import ArchiveItem
from lib.data.archive_repository import ArchiveRepository
from lib.mail import EMail
from pathlib import Path
from werkzeug.exceptions import NotFound

app = Flask(__name__)
app.config.from_object("config.Config")

aws_auth = AWSCognitoAuthentication(app)

MYSQL_PROPS = {
    "user": app.config["MYSQL_USER"],
    "password": app.config["MYSQL_PASSWORD"],
    "host": app.config["MYSQL_HOST"],
    "db": app.config["MYSQL_DATABASE"],
}

LOGOUT_URL = f"https://{app.config['AWS_COGNITO_DOMAIN']}/logout?client_id={app.config['AWS_COGNITO_USER_POOL_CLIENT_ID']}&logout_uri={app.config['AWS_COGNITO_LOGOUT_URL']}"

ALL_QUEUES = app.config["QUEUES"]


def ensure_signin(view):
    @ wraps(view)
    def decorated(*args, **kwargs):
        access_token = request.cookies.get("access_token")
        if access_token == None:
            return redirect("/sign_in")

        return view(access_token, *args, **kwargs)

    return decorated


@ app.route("/", methods=["GET"])
@ ensure_signin
def home(access_token):

    username = request.cookies.get("username")
    return render_template(
        "index.html",
        access_token=access_token,
        username=username,
        LOGOUT_URL=LOGOUT_URL,
        queue_name=ALL_QUEUES[0],
        email_template=Path('templates/email.html').read_text()
    )


@app.route("/users", methods=["GET"])
@ensure_signin
def users(access_token):

    username = request.cookies.get("username")
    return render_template("users.html", access_token=access_token, username=username)


@app.route("/archive", methods=["GET"])
@ensure_signin
def archive(access_token):

    username = request.cookies.get("username")
    return render_template("archive.html", access_token=access_token, username=username)


@app.route("/corrections/<string:queue_name>", methods=["GET"])
@aws_auth.authentication_required
def get_correction(queue_name):
    table = get_correction_table()

    try_get = True
    start_key = None
    while try_get:
        try_get = False
        expires = datetime.utcnow() - timedelta(minutes=1)
        values = {
            ":q1": queue_name,
            ":t1": Decimal(expires.timestamp()),
            ":v1": 0,
            ":null": None,
        }
        key_condition = "queue = :q1"
        filter = "version = :v1 or lastAssigned = :null or lastAssigned < :t1"
        if start_key:
            response = table.query(
                ExpressionAttributeValues=values,
                KeyConditionExpression=key_condition,
                FilterExpression=filter,
                Limit=1,
                ExclusiveStartKey=start_key,
            )
        else:
            response = table.query(
                ExpressionAttributeValues=values,
                KeyConditionExpression=key_condition,
                FilterExpression=filter,
                Limit=1,
            )
        correction = next(iter(response["Items"]), None)
        if not correction and "LastEvaluatedKey" in response:
            start_key = response["LastEvaluatedKey"]
            try_get = True
        elif correction:
            old_version = correction.get("version", 0)
            correction["version"] = old_version + 1
            now = datetime.utcnow().timestamp()
            try:
                table.update_item(
                    Key={"queue": correction["queue"], "id": correction["id"]},
                    ExpressionAttributeValues={
                        ":v1": old_version,
                        ":v2": correction["version"],
                        ":t1": Decimal(now),
                    },
                    UpdateExpression="SET version = :v2, lastAssigned = :t1",
                    ConditionExpression="version = :v1",
                )
                correction["version"] = int(correction["version"])
                correction["lastAssigned"] = now
            except ClientError as e:
                if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                    print("Conflict occurred while reserving the item. Trying again...")
                    time.sleep(0.1)
                    try_get = True

    return jsonify(correction)


@app.route("/hadiths/<int:urn>", methods=["GET"])
@aws_auth.authentication_required
def get_hadith(urn: int):
    response = requests.get(
        f"https://api.sunnah.com/v1/hadiths/{urn}",
        headers={
            "Content-Type": "application/json",
            "X-API-KEY": app.config.get("SUNNAH_COM_API_KEY"),
        },
    )

    if response.status_code == 200:
        return response.content
    else:
        return NotFound()


@app.route("/queues/", methods=["GET"])
@aws_auth.authentication_required
def get_queues():
    queues = [{"name": name} for name in ALL_QUEUES]
    return jsonify(queues)


@app.route("/corrections/<string:queue_name>/<string:correction_id>", methods=["POST"])
@aws_auth.authentication_required
def resolve_correction(queue_name, correction_id):
    data = request.json
    if "action" not in data or (
        data["action"] == "approve" and "corrected_val" not in data
    ):
        return jsonify(
            create_response_message(
                False,
                'Please provide valid action param "reject", "skip", or "approve" and "corrected_val" param',
            )
        )

    action = data["action"]
    email_template = data.get("emailTemplate", "")
    version = data.get("version", 0)
    username = request.cookies.get("username")

    if action == "reject":
        return reject_correction(
            queue_name, correction_id, email_template, version, username
        )

    elif action == "approve":
        return approve_correction(
            queue_name,
            correction_id,
            email_template,
            version,
            username,
            data["corrected_val"]
        )

    elif action == "skip":
        return skip_correction(queue_name, correction_id, version, username)

    elif action == "move":
        return move_correction(
            queue_name, correction_id, version, data["new_queue_name"]
        )

    else:
        return jsonify(
            create_response_message(
                False,
                'Please provide valid action param "reject", "skip", or "approve".',
            )
        )


@app.route("/logout")
def logout():
    response = make_response(redirect("/"))
    response.set_cookie("username", "", expires=0)
    response.set_cookie("access_token", "", expires=0)
    return response


@app.route("/aws_cognito_redirect")
def aws_cognito_redirect():
    access_token = aws_auth.get_access_token(request.args)
    response = make_response(redirect("/"))
    expires = datetime.utcnow() + timedelta(minutes=10)
    aws_auth.token_service.verify(access_token)
    response.set_cookie(
        "username",
        aws_auth.token_service.claims["username"],
        expires=expires,
        httponly=True,
    )
    response.set_cookie("access_token", access_token,
                        expires=expires, httponly=True)
    return response


@app.route("/sign_in")
def sign_in():
    return redirect(aws_auth.get_sign_in_url())


def reject_correction(queue_name, correction_id, email_template, version, username):
    correction = read_correction(queue_name, correction_id, version)
    if not correction:
        return not_found(correction_id)
    if email_template:
        send_email("rejected", correction, email_template)
    return archive_correction(
        queue_name, correction_id, version, username
    )


def approve_correction(
    queue_name, correction_id, email_template, version, username, corrected_val
):
    try:
        correction = read_correction(queue_name, correction_id, version)
        if not correction:
            return not_found(correction_id)

        attribute = contruct_correction_attribute(
            correction["attr"]
        )
        if not attribute:
            return invalid_attribute(correction_id)
        
        rows_affected = save_correction_to_hadith_table(
            correction["urn"], corrected_val, attribute
        )

        if rows_affected > 0:
            archive_correction(
                queue_name,
                correction_id,
                username,
                corrected_val,
                attribute,
                True,
            )

            if email_template:
                send_email("approved", correction,
                           email_template, corrected_val)
            return jsonify(
                create_response_message(
                    True, "Successfully updated hadith")
            )
        else:
            return jsonify(
                create_response_message(False, "Failed to update hadith")
            )

    except ClientError as e:
        return jsonify(create_response_message(False, e.response["Error"]["Message"]))
    except pymysql.Error as error:
        return jsonify(create_response_message(False, str(error)))
    except Exception as exception:
        return jsonify(create_response_message(False, "Error - " + str(exception)))


def contruct_correction_attribute(attribute: str):
    switcher = {
        "body": "hadithText",
        "grade": "grade",
    }
    res = switcher.get(attribute, "")
    if res == "":
        return None
    return res

  
def get_dynamo_db():
    dynamodb = boto3.resource(
        "dynamodb",
        endpoint_url=app.config["DYNAMODB_ENDPOINT_URL"],
        region_name=app.config["REGION"],
    )
    return dynamodb


def send_email(
    decision: str,
    correction: dict,
    email_template: str = "",
    corrected_val: str = "",
):
    email = EMail()
    ctx = dict(
        **correction,
        **{"decision": decision, "correctedVal": corrected_val},
    )
    email.send(
        template=email_template,
        ctx=ctx,
        subject=f"Your correction has been {decision}",
        recipients=[correction["submittedBy"]],
    )


def save_correction_to_hadith_table(urn: int, corrected_val: str, attribute: str):
    conn = pymysql.connect(**MYSQL_PROPS)
    cursor = conn.cursor()
    query = "UPDATE bukhari_english SET {attr} = %(corrected_val)s WHERE englishURN = %(urn)s".format(attr = attribute)
    cursor.execute(query, {"corrected_val": corrected_val, "urn": urn})
    rows_affected = cursor.rowcount
    conn.commit()
    conn.close()
    return rows_affected


def get_correction_table():
    return get_dynamo_db().Table(app.config["DYNAMODB_TABLE"])


def read_correction(queue_name, correction_id, version):
    response = get_correction_table().get_item(
        Key={"queue": queue_name, "id": str(correction_id)}
    )
    if not ("Item" in response and response["Item"].get("version", 0) == version):
        return None
    return response["Item"]


def delete_correction(queue_name, correction_id, version):
    get_correction_table().delete_item(
        Key={"queue": queue_name, "id": str(correction_id)},
        ExpressionAttributeValues={":v1": version},
        ConditionExpression="version = :v1",
    )


def move_correction(queue_name, correction_id, version, updated_queue_name):
    correction = read_correction(queue_name, correction_id, version)
    if not correction:
        return not_found(correction_id)
    delete_correction(queue_name, correction_id, version)
    try:
        # it is not possible to update an attribute that forms the Key of an item thus we need to copy/delete/put in DynamoDB.
        correction = reset_correction(correction)
        correction["queue"] = updated_queue_name
        get_correction_table().put_item(Item=correction)
        return jsonify(create_response_message(True, "Successfully moved correction."))
    except Exception as exception:
        return jsonify(create_response_message(False, "Error - " + str(exception)))


def skip_correction(queue_name, correction_id, version, username):
    correction = read_correction(queue_name, correction_id, version)
    if not correction:
        return not_found(correction_id)
    delete_correction(queue_name, correction_id, version)
    try:
        correction = reset_correction(correction)
        get_correction_table().put_item(Item=correction)
        return jsonify(create_response_message(True, "Success"))
    except ClientError as e:
        return jsonify(create_response_message(False, e.response["Error"]["Message"]))


def archive_correction(
    queue_name: str,
    correction_id: str,
    version: int,
    username: str,
    corrected_val: str = None,
    attribute: str = None,
    approved: bool = False,
):
    archive_repository = ArchiveRepository(
        app.config["DYNAMODB_ENDPOINT_URL"],
        app.config["REGION"],
        app.config["DYNAMODB_TABLE_ARCHIVE"],
    )

    try:
        correction = read_correction(queue_name, correction_id, version)
        if not correction:
            return not_found(correction_id)

        entry = dict(
            **correction,
            **{
                "modifiedBy": username,
                "correctedVal": corrected_val,
                "attribute": attribute,
                "approved": approved,
            },
        )
        archive_repository.write(ArchiveItem.deserialize(entry))
        delete_correction(queue_name, correction_id, version)
    except ClientError as e:
        return jsonify(create_response_message(False, e.response["Error"]["Message"]))

    return jsonify(create_response_message(True, "Success"))


def not_found(correction_id):
    return jsonify(
        create_response_message(
            False, f'Correction with id "{correction_id}" not found.'
        )
    )

def invalid_attribute(correction_id):
    return jsonify(
        create_response_message(
            False, f'Correction with id "{correction_id}" has invalid attribute'
        )
    )


def create_response_message(success, message):
    return {"success": success, "message": message}


def extensions(app):
    """
    Register 0 or more extensions (mutates the app passed in).

    :param app: Flask application instance
    :return: None
    """
    mail.init_app(app)

    return None


def reset_correction(correction):
    # format of id is timestamp:aws_request_id where first part is date and second part is random string
    reset_fields = ["id", "version", "lastAssigned"]
    if "id" in reset_fields:
        aws_request_id = next(iter(correction["id"].split(":", 1)[1:]), "")
        correction["id"] = f"{time.time()}:{aws_request_id}"
    if "version" in reset_fields:
        correction["version"] = 0
    if "lastAssigned" in reset_fields:
        correction.pop("lastAssigned", None)
    return correction


with app.app_context():
    extensions(app)

if __name__ == "__main__":
    app.run(host="0.0.0.0")
