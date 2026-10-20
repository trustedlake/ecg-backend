from django.db import migrations


class Migration(migrations.Migration):
    """English field names."""

    dependencies = [
        ("uploads", "0001_initial"),
    ]

    operations = [
        migrations.RenameField(model_name="uploadbatch", old_name="confirmado", new_name="confirmed"),
        migrations.RenameField(model_name="uploadbatch", old_name="confirmado_em", new_name="confirmed_at"),
        migrations.RenameField(model_name="uploadedfile", old_name="nome_original", new_name="original_name"),
    ]
