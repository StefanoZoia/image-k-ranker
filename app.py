from flask import Flask, render_template, request, jsonify, current_app
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy import select
from flask_migrate import Migrate
import os
import random
import logging
import json

logging.basicConfig(level=logging.DEBUG)

app = Flask(__name__)

image_sets = []
descriptions = None

# DB settings

uri = os.environ.get("DATABASE_URL")
if uri and uri.startswith("postgres://"):
    uri = uri.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = uri

app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy()
migrate = Migrate()
db.init_app(app)
migrate.init_app(app, db)

class UserSession(db.Model):
    __tablename__ = "user_session"
    id = db.Column(db.Integer, primary_key=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    img_sequence = db.Column(db.JSON, nullable=False)
    next_idx = db.Column(db.Integer, nullable=False, default=0)
    evaluations = db.relationship(
        "Evaluation",
        backref="session",
        cascade="all, delete-orphan"
    )

class Evaluation(db.Model):
    __tablename__ = "evaluation"
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer,
                           db.ForeignKey("user_session.id"),
                           nullable=False,
                           index=True)
    image_basename = db.Column(db.String, nullable=False)
    img_set = db.Column(db.JSON, nullable=False)
    answer = db.Column(db.JSON, nullable=False)
    comment = db.Column(db.String, nullable=False)
    timestamp = db.Column(db.DateTime, server_default=db.func.now())
    __table_args__ = (
        db.UniqueConstraint("session_id", "image_basename",
                            name="uq_session_imageset"),
    )


def list_images(path):
    return {
        f for f in os.listdir(path)
           if os.path.isfile(os.path.join(path, f))
           and f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
    }

def initialize_image_sets():
    IMGS_FOLDER = "static/generated_images"
    systems_dirs = os.listdir(IMGS_FOLDER)
    
    global image_sets
    sets = dict()

    img_sets_list = [list_images(f"{current_app.root_path}/{IMGS_FOLDER}/{d}") for d in systems_dirs]
    common = set.intersection(*img_sets_list)

    for img in common:
        basename = os.path.splitext(os.path.basename(img))[0]
        sets[basename] = [f"/{IMGS_FOLDER}/{d}/{img}" for d in systems_dirs]

    image_sets = sets

    global descriptions
    with open("static/descriptions.json", encoding="utf-8") as descrfie:
        descriptions = json.load(descrfie)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/get_session')
def get_new_session():

        global image_sets

        # shuffle image pairs
        sequence = list(image_sets.keys())
        random.shuffle(sequence)
        print(len(sequence))

        u_session = UserSession(img_sequence=sequence)
        db.session.add(u_session)
        db.session.commit()
        new_session = u_session.id
        
        app.logger.info(f"New session {new_session}: {sequence}")

        return jsonify({'session': new_session})
    
@app.route('/get_images', methods=['POST'])
def get_images():
    data = request.json

    # read user session data
    u_session_id = data['sessionId']
    stmt = select(UserSession).where(UserSession.id == u_session_id)
    user_session = db.session.scalars(stmt).first()
    if user_session is None:
        return jsonify({"error": f"session number {u_session_id} not found"}), 400
    session_sequence = user_session.img_sequence
    total_pairs = len(session_sequence)
    session_curr_index = user_session.next_idx


    if session_curr_index >= total_pairs:
        return jsonify({'end': "Thank you for evaluating all the images in our dataset!",
                        'progress': {
                            'current': session_curr_index,
                            'total': total_pairs
                        }})

    # read next images from the preordered list for this session
    basename = session_sequence[session_curr_index]
    images = image_sets[basename]
    
    # randomize presentation order of images
    random.shuffle(images)

    return jsonify({
        'images':  images,
        'description': descriptions[basename],
        'progress': {
            'current': session_curr_index,
            'total': total_pairs
        }
    })


@app.route('/update_scores', methods=['POST'])
def update_scores():

    data = request.json
    images = data['images']
    ans = data['answer']
    comm = data['comment']
    u_session_id = data['sessionId']

    img_basename = images[0].split("/")[-1]

    # get user session data
    stmt = select(UserSession).where(UserSession.id == u_session_id)
    user_session = db.session.scalars(stmt).first()
    if user_session is None:
        return jsonify({"error": f"session number {u_session_id} not found"}), 400
    
    try:
        # save the new evaluation
        db.session.add(Evaluation(
            session_id = u_session_id,
            image_basename = img_basename,
            img_set = images,
            answer = ans,
            comment = comm
        ))

        # update user session data
        user_session.next_idx += 1

        # commit to db
        db.session.commit()

    except IntegrityError:
        db.session.rollback()
        return jsonify({"status": "already_submitted"}), 200
    
    except SQLAlchemyError as e:
        db.session.rollback()
        app.logger.error(f"DB error: {e}")
        return jsonify({"error": "Database error"}), 500

    app.logger.info(f"Updated session {u_session_id}: next index {user_session.next_idx}")
    
    return jsonify({"status": "ok"}), 200


@app.route("/health")
def health():
    return "OK"


with app.app_context():
    initialize_image_sets()

if __name__ == '__main__':
    app.run(debug=True, threaded=True)