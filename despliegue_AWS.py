import json
import boto3
import time
import zipfile
import io
import uuid
import os

# CONFIGURACION
REGION = 'us-east-1'
# Detectar el ID de cuenta automaticamente
try:
    sts = boto3.client('sts', region_name=REGION)
    account_id = sts.get_caller_identity()['Account']
    # Construir el ARN usando el ID de cuenta del usuario
    ROLE_ARN = f"arn:aws:iam::{account_id}:role/LabRole"
    print(f"Rol detectado automaticamente: {ROLE_ARN}")
except Exception as e:
    print("No se pudo detectar la cuenta automaticamente. Revisa tus credenciales.")

# Generamos IDs únicos
ID_PROYECTO = str(uuid.uuid4())[:8]
BUCKET_ENTRADA = f'proyecto-entrada-{ID_PROYECTO}'
BUCKET_WEB = f'proyecto-web-{ID_PROYECTO}'
TABLA_DYNAMO = 'TransripcionesAuto'
NOMBRE_LAMBDA = f'procesador-imagenes-{ID_PROYECTO}'

# Clientes
s3 = boto3.client('s3', region_name=REGION)
dynamodb = boto3.client('dynamodb', region_name=REGION)
lambda_client = boto3.client('lambda', region_name=REGION)

def crear_infraestructura():
    print(f"INICIANDO DESPLIEGUE TOTAL... ID: {ID_PROYECTO}")

    # DYNAMODB
    print("Verificando DynamoDB...")
    try:
        dynamodb.create_table(
            TableName=TABLA_DYNAMO,
            KeySchema=[{'AttributeName': 'id_archivo', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 'id_archivo', 'AttributeType': 'S'}],
            BillingMode='PAY_PER_REQUEST'
        )
        time.sleep(5) # Espera de seguridad
    except Exception:
        print("   (La tabla ya existía, seguimos...)")

    # BUCKETS
    print("Creando Buckets...")
    for bucket in [BUCKET_ENTRADA, BUCKET_WEB]:
        print(f"Creando: {bucket}...")
        try:
            s3.create_bucket(Bucket=bucket)

            # Esperar a que exista
            # Esto pausa el script hasta que AWS confirme que el bucket esta listo
            print("Esperando confirmacion de AWS...")
            waiter = s3.get_waiter('bucket_exists')
            waiter.wait(Bucket=bucket)

            s3.put_bucket_cors(
                Bucket=bucket,
                CORSConfiguration={
                    'CORSRules': [{
                        'AllowedHeaders': ['*'],
                        'AllowedMethods': ['GET', 'PUT', 'POST', 'HEAD'],
                        'AllowedOrigins': ['*'],
                        'ExposeHeaders': ['ETag']
                    }]
                }
            )
            print("Bucket listo y configurado.")

        except Exception as e:
            print(f"Error creando {bucket}: {e}")
            # Si falla no se configura el sitio web
            if bucket == BUCKET_WEB:
                print("Saltando configuracion web por error previo.")
                continue
    
    # Configurar Hosting Web
    try:
        print("Activando Hosting Web...")
        s3.put_bucket_website(
            Bucket=BUCKET_WEB, 
            WebsiteConfiguration={'IndexDocument': {'Suffix': 'index.html'}}
        )
    except Exception as e:
        print(f"No se pudo activar hosting (¿Falló la creación del bucket?): {e}")

    # LAMBDA
    print("3️Subiendo Lambda...")
    if not os.path.exists('lambda_function.py'):
        print("ERROR: Falta el archivo lambda_function.py")
        return

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.write('lambda_function.py', arcname='lambda_function.py')
    zip_buffer.seek(0)

    lambda_arn = ""
    try:
        response = lambda_client.create_function(
            FunctionName=NOMBRE_LAMBDA,
            Runtime='python3.9',
            Role=ROLE_ARN,
            Handler='lambda_function.lambda_handler',
            Code={'ZipFile': zip_buffer.read()},
            Timeout=15,
            Environment={'Variables': {'TABLA_DYNAMO': TABLA_DYNAMO}}
        )
        lambda_arn = response['FunctionArn']
        print(f"Lambda creada: {NOMBRE_LAMBDA}")
    except Exception as e:
        print(f"Error Lambda: {e}")
        return

    # TRIGGER S3
    print("Conectando el Trigger...")
    try:
        # Dar permiso a S3 para invocar la Lambda
        lambda_client.add_permission(
            FunctionName=NOMBRE_LAMBDA,
            StatementId=f's3-invoke-{ID_PROYECTO}',
            Action='lambda:InvokeFunction',
            Principal='s3.amazonaws.com',
            SourceArn=f"arn:aws:s3:::{BUCKET_ENTRADA}"
        )
        
        # Espera de seguridad
        print("Esperando propagacion de permisos (5s)...")
        time.sleep(5)

        # Configurar notificacion
        s3.put_bucket_notification_configuration(
            Bucket=BUCKET_ENTRADA,
            NotificationConfiguration={
                'LambdaFunctionConfigurations': [{
                    'LambdaFunctionArn': lambda_arn,
                    'Events': ['s3:ObjectCreated:*']
                }]
            }
        )
        print("Trigger conectado correctamente.")
    except Exception as e:
        print(f"Error en Trigger: {e}")

    # Automatizar publicacion web
    print("Publicando pagina web...")
    try:
        # Leer el index.html
        with open('index.html', 'r', encoding='utf-8') as f:
            html_content = f.read()
        
        # Reemplazar el marcador por el nombre real del bucket nuevo
        html_final = html_content.replace('__NOMBRE_BUCKET_PLACEHOLDER__', BUCKET_ENTRADA)
        
        # Subir el archivo modificado directamente a S3
        s3.put_object(
            Bucket=BUCKET_WEB,
            Key='index.html',
            Body=html_final,
            ContentType='text/html'
        )
        print("index.html actualizado y subido.")

        # Hacer publico el Bucket Web
        print("Intentando abrir permisos publicos del Bucket Web...")
        
        # Quitar el bloqueo de acceso publico
        try:
            s3.delete_public_access_block(Bucket=BUCKET_WEB)
            print("      - Bloqueo de acceso público eliminado.")
            time.sleep(2) # Esperar a que AWS procese el cambio
        except Exception as e:
            print(f"No se pudo quitar el bloqueo. {e}")

        # Aplicar la politica de lectura publica
        policy_json = {
            "Version": "2012-10-17",
            "Statement": [{
                "Sid": "PublicReadGetObject",
                "Effect": "Allow",
                "Principal": "*",
                "Action": "s3:GetObject",
                "Resource": f"arn:aws:s3:::{BUCKET_WEB}/*"
            }]
        }
        
        try:
            s3.put_bucket_policy(
                Bucket=BUCKET_WEB,
                Policy=json.dumps(policy_json)
            )
            print("Politica publica aplicada correctamente.")
        except Exception as e:
            print(f"AWS Academy bloquep la creacion de la politica: {e}")

        except Exception as e:
            print(f"Error general en paso web: {e}")

        print("\n PROYECTO DESPLEGADO AL 100%")    
        
        # Hacer publico el objeto
        try:
            s3.put_object_acl(Bucket=BUCKET_WEB, Key='index.html', ACL='public-read')
        except:
            print("No se pudo hacer publico automático, verifica permisos manualmente.")

    except Exception as e:
        print(f"Error subiendo web: {e}")

    print("\nPROYECTO DESPLEGADO AL 100%")
    print(f"URL: http://{BUCKET_WEB}.s3-website-{REGION}.amazonaws.com")
    print(f"Bucket Fotos: {BUCKET_ENTRADA}")

if __name__ == '__main__':
    crear_infraestructura()