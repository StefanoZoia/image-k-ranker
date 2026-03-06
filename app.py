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

image_pairs = []
control_pairs = []


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
    next_pair = db.Column(db.Integer, nullable=False, default=0)
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
    image_a = db.Column(db.String, nullable=False)
    image_b = db.Column(db.String, nullable=False)
    winner_image = db.Column(db.String, nullable=False)
    winner_position = db.Column(db.String, nullable=False)
    timestamp = db.Column(db.DateTime, server_default=db.func.now())
    __table_args__ = (
        db.UniqueConstraint("session_id", "image_a", "image_b",
                            name="uq_session_imagepair"),
    )


def list_images(path):
    return {
        f for f in os.listdir(path)
           if os.path.isfile(os.path.join(path, f))
           and f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
    }

def initialize_image_pairs():
    SYSTEMS_DIRS = ["cocos_images", "images"]
    CONTROL_DIRS = ["control_winners", "control_losers"]
    
    global image_pairs
    pairs = dict()

    dir0 = list_images(os.path.join(current_app.root_path, "static", SYSTEMS_DIRS[0]))
    dir1 = list_images(os.path.join(current_app.root_path, "static", SYSTEMS_DIRS[1]))
    common = dir0.intersection(dir1)

    for img in common:
        img_path_0 = f"/static/{SYSTEMS_DIRS[0]}/{img}"
        img_path_1 = f"/static/{SYSTEMS_DIRS[1]}/{img}"
        basename = os.path.splitext(os.path.basename(img))[0]
        pairs[basename] = (img_path_0, img_path_1)

    image_pairs = pairs
    
    global control_pairs
    pairs = dict()

    dir0 = list_images(os.path.join(current_app.root_path, "static", CONTROL_DIRS[0]))
    dir1 = list_images(os.path.join(current_app.root_path, "static", CONTROL_DIRS[1]))
    common = dir0.intersection(dir1)

    for img in common:
        img_path_0 = f"/static/{CONTROL_DIRS[0]}/{img}"
        img_path_1 = f"/static/{CONTROL_DIRS[1]}/{img}"
        basename = os.path.splitext(os.path.basename(img))[0]
        pairs[basename] = (img_path_0, img_path_1)

    control_pairs = pairs

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/get_session')
def get_new_session():
        # one control image every BATCH_LEN evaluation images
        BATCH_LEN = 5

        global image_pairs
        global control_pairs

        # shuffle image pairs
        basenames = list(image_pairs.keys())
        random.shuffle(basenames)
        print(len(basenames))

        control_basenames = list(control_pairs.keys())
        random.shuffle(control_basenames)
        print(len(control_basenames))

        # build sequence
        sequence = list()
        i = 0  #batch index
        j = 0  #basenames index
        while j < len(basenames):
            # read the next BATCH_LEN regular images
            batch = basenames[j : j + BATCH_LEN]
            j += BATCH_LEN

            # insert next control image in random position
            control_image = control_basenames[i % len(control_basenames)]
            batch.insert(random.randrange(BATCH_LEN+1), control_image)

            # append this batch to the session sequence
            sequence.extend(batch)
            i += 1
            

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
    session_curr_pair = user_session.next_pair


    if session_curr_pair >= total_pairs:
        return jsonify({'end': "Thank you for evaluating all the images in our dataset!",
                        'progress': {
                            'current': session_curr_pair,
                            'total': total_pairs
                        }})

    # read next pair from the preordered list for this session
    basename = session_sequence[session_curr_pair]
    img1, img2 = image_pairs[basename] if basename in image_pairs else control_pairs[basename]

    img_name = os.path.splitext(os.path.basename(img1))[0]
    descr_filename = os.path.join(current_app.root_path,
                                  "static", "refined-jsons", f"{img_name}.json")
    img_info = None

    try:
        with open(descr_filename, encoding="utf-8") as f:
            img_info = json.load(f)
    except FileNotFoundError:
        return jsonify({"error": f"Missing description for {img_name}"}), 500
    
    # randomize presentation order of images
    if random.choice([True, False]):
        img1, img2 = img2, img1

    # build a single string representing the description of the image
    descriptions = [d.rstrip(".") for d in img_info["descriptions"]]
    descr = f'"{" and ".join(descriptions)}"'

    return jsonify({
        'image1':  img1,
        'image2':  img2,
        'description': descr,
        'progress': {
            'current': session_curr_pair,
            'total': total_pairs
        }
    })


@app.route('/update_scores', methods=['POST'])
def update_scores():

    data = request.json
    image1 = data['image1']
    image2 = data['image2']
    winner = data['winner']
    u_session_id = data['sessionId']

    img_a, img_b = sorted([image1, image2])
    winner_img = None
    winner_pos = None
    if winner == "image1":
        winner_img = image1
        winner_pos = "left"
    elif winner == "image2":
        winner_img = image2
        winner_pos = "right"
    else:
        return jsonify({"error": f"winner {winner} not valid"}), 500

    # get user session data
    stmt = select(UserSession).where(UserSession.id == u_session_id)
    user_session = db.session.scalars(stmt).first()
    if user_session is None:
        return jsonify({"error": f"session number {u_session_id} not found"}), 400
    
    try:
        # save the new evaluation
        db.session.add(Evaluation(
            session_id = u_session_id,
            image_a = img_a,
            image_b = img_b,
            winner_image = winner_img,
            winner_position = winner_pos
        ))

        # update user session data
        user_session.next_pair += 1

        # commit to db
        db.session.commit()

    except IntegrityError:
        db.session.rollback()
        return jsonify({"status": "already_submitted"}), 200
    
    except SQLAlchemyError as e:
        db.session.rollback()
        app.logger.error(f"DB error: {e}")
        return jsonify({"error": "Database error"}), 500

    app.logger.info(f"Updated session {u_session_id}: next pair {user_session.next_pair}")
    
    return jsonify({"status": "ok"}), 200


@app.route("/health")
def health():
    return "OK"


with app.app_context():
    initialize_image_pairs()

if __name__ == '__main__':
    app.run(debug=True, threaded=True)