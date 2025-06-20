from flask_wtf import FlaskForm
from wtforms import StringField, SelectField, SubmitField
from wtforms.validators import DataRequired, Regexp

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
