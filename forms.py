from flask_wtf import FlaskForm
from wtforms import StringField, SelectField, FileField, SubmitField
from wtforms.validators import DataRequired, Regexp, Optional, InputRequired

class IgImportForm(FlaskForm):
    package_name = StringField('Package Name', validators=[
        DataRequired(),
        Regexp(r'^[a-zA-Z0-9][a-zA-Z0-9\-\.]*[a-zA-Z0-9]$', message="Invalid package name format.")
    ], render_kw={'placeholder': 'e.g., hl7.fhir.au.core'})
    package_version = StringField('Package Version', validators=[
        DataRequired(),
        Regexp(r'^[a-zA-Z0-9\.\-]+$', message="Invalid version format.")
    ], render_kw={'placeholder': 'e.g., 1.1.0-preview'})
    dependency_mode = SelectField('Dependency Mode', choices=[
        ('recursive', 'Current Recursive'),
        ('patch-canonical', 'Patch Canonical Versions'),
        ('tree-shaking', 'Tree Shaking')
    ], default='recursive')
    submit = SubmitField('Import')

class ContributeTestDataForm(FlaskForm):
    test_file = FileField('Test Data File (JSON)', validators=[
        InputRequired("Please select a JSON file."),
        lambda form, field: field.data.filename.lower().endswith('.json') or form.test_file.errors.append('File must be JSON.')
    ], render_kw={'accept': '.json'})
    contributor = StringField('Contributor Name', validators=[DataRequired()], render_kw={'placeholder': 'Your Name'})
    submit = SubmitField('Submit Test Data')