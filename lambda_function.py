import json
import boto3
import os
import urllib.parse

# Inicializamos clientes
rekognition = boto3.client('rekognition')
dynamodb = boto3.resource('dynamodb')

# Leemos el nombre de la tabla desde la configuracion de despliegue
NOMBRE_TABLA = os.environ.get('TABLA_DYNAMO')
table = dynamodb.Table(NOMBRE_TABLA)

def lambda_handler(event, context):
    print("Evento recibido:", json.dumps(event))
    
    # Obtener bucket y nombre de archivo
    try:
        bucket = event['Records'][0]['s3']['bucket']['name']
        key = urllib.parse.unquote_plus(event['Records'][0]['s3']['object']['key'])
        
        print(f"Analizando imagen: {key} en bucket: {bucket}")
    except KeyError:
        return {'statusCode': 400, 'body': 'No es un evento S3 valido'}

    try:
        # Llamar a Rekognition para detectar etiquetas
        response = rekognition.detect_labels(
            Image={'S3Object': {'Bucket': bucket, 'Name': key}},
            MaxLabels=10,
            MinConfidence=75
        )
        
        etiquetas = [label['Name'] for label in response['Labels']]
        print(f"Etiquetas: {etiquetas}")
        
        # Guardar en DynamoDB
        table.put_item(Item={
            'id_archivo': key,
            'contenido': etiquetas,
            'estado': 'Procesado'
        })
        
        return {
            'statusCode': 200,
            'body': json.dumps(etiquetas)
        }

    except Exception as e:
        print(f"Error: {e}")
        raise e