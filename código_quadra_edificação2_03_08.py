from qgis.core import (
    QgsProject,
    QgsFeature,
    QgsFeatureRequest,
    QgsSpatialIndex,
    QgsGeometry,
    Qgis,
    QgsWkbTypes,
    NULL,
    QgsGeometryEngine,
    QgsProviderRegistry
)
from qgis.utils import iface
from qgis.PyQt.QtWidgets import QInputDialog

# ==========================================
# 1. NOMES DAS CAMADAS NO PROJETO DO QGIS
# ==========================================
NOME_CAMADA_EDIF = 'ct_edificacao_fiscal'
NOME_CAMADA_LOTES = 'ct_lote_fiscal'
NOME_CAMADA_QUADRAS = 'ct_quadra_fiscal'
NOME_CAMADA_SETORES = 'ct_setor_fiscal'

NOME_CAMPO_SETOR = 'cod_sf'

# ================================================================
# CONFIGURAÇÃO DAS QUADRAS CRIADAS PARA EDIFICAÇÕES ISOLADAS
# ================================================================
# A margem é aplicada ao redor da edificação para que a nova quadra
# fique ligeiramente maior que o lote. Use 0.0 para a quadra ter a
# mesma geometria da edificação. O valor está na unidade do projeto
# (normalmente metros em uma camada UTM).
MARGEM_NOVA_QUADRA = 0.001

# A trigger atualizar_sq_quadra preenche cod_sf, cod_sf_sat, cod_qf e sq
# no banco. O script não atribui manualmente esses campos às novas quadras.
CRIAR_QUADRA_PARA_EDIFICACAO_ISOLADA = True

# Quadras existentes que precisam ser ampliadas acionam a trigger de
# sobreposição no banco. Mantenha False enquanto houver sobreposições
# preexistentes a serem saneadas na base.
AMPLIAR_QUADRAS_EXISTENTES = False

# Mantenha False para que o Console Python mostre somente conflitos que a
# trigger de sobreposição poderia rejeitar. Altere para True apenas quando
# for necessário investigar as demais regras do processamento.
EXIBIR_DIAGNOSTICOS_COMPLETOS = False

def diagnostico(*mensagens):
    """Imprime mensagens operacionais somente no modo detalhado."""
    if EXIBIR_DIAGNOSTICOS_COMPLETOS:
        print(*mensagens)

def obter_camada_do_projeto(nome_camada):
    camadas = QgsProject.instance().mapLayersByName(nome_camada)
    return camadas[0] if camadas else None

def validar_campos_obrigatorios(camadas_campos):
    for camada, campos in camadas_campos.items():
        faltantes = [
            campo for campo in campos
            if camada.fields().indexOf(campo) == -1
        ]

        if faltantes:
            iface.messageBar().pushMessage(
                'Erro',
                f'Camada {camada.name()}: campos ausentes: {", ".join(faltantes)}',
                level=Qgis.Critical,
                duration=10
            )
            return False

    return True

def escolher_setores_para_processar(layer_setores):
    """Exibe os setores disponíveis e retorna somente os escolhidos."""
    setores_por_codigo = {}

    for setor in layer_setores.getFeatures():
        valor_cod_sf = setor.attribute(NOME_CAMPO_SETOR)
        if valor_cod_sf in (None, NULL):
            continue

        codigo = str(valor_cod_sf).strip()
        if codigo:
            setores_por_codigo.setdefault(codigo, []).append(setor)

    if not setores_por_codigo:
        return None

    def chave_ordenacao(codigo):
        try:
            return (0, int(codigo))
        except (TypeError, ValueError):
            return (1, codigo)

    codigos_disponiveis = sorted(setores_por_codigo, key=chave_ordenacao)
    opcoes = codigos_disponiveis + ['Todos os setores']
    escolha, confirmou = QInputDialog.getItem(
        iface.mainWindow(),
        'Processar setor fiscal',
        'Selecione o setor fiscal:',
        opcoes,
        0,
        False
    )

    if not confirmou:
        return []

    if escolha == 'Todos os setores':
        return [
            setor
            for codigo in codigos_disponiveis
            for setor in setores_por_codigo[codigo]
        ]

    return setores_por_codigo[escolha]

def obter_sq_do_lote(feat_lote, indice_sq, indice_sql):
    """Lê o SQ do lote sem atribuí-lo; o banco o gera nos lotes novos."""
    if indice_sq != -1:
        valor_sq = feat_lote.attribute(indice_sq)
        if valor_sq not in (None, NULL) and str(valor_sq).strip():
            return str(valor_sq).strip()

    valor_sql = feat_lote.attribute(indice_sql)
    if valor_sql not in (None, NULL):
        texto_sql = str(valor_sql).strip()
        if len(texto_sql) >= 7:
            return texto_sql[:7]

    return None

def ajustar_geometria_para_camada(geometria, eh_multi):
    """Adapta a geometria ao tipo Polygon/MultiPolygon da camada."""
    geometria = QgsGeometry(geometria)

    if eh_multi and not geometria.isMultipart():
        geometria.convertToMultiType()
    elif not eh_multi and geometria.isMultipart():
        maior_area = -1
        geometria_simples = geometria

        for parte in geometria.asGeometryCollection():
            if parte.area() > maior_area:
                maior_area = parte.area()
                geometria_simples = parte

        geometria = geometria_simples

    return geometria

def encontrar_colisao_com_quadra(
    geometrias_finais,
    geometria_teste,
    sq_teste,
    considerar_sq_diferente=True
):
    """Impede contatos de interior entre quadras com SQs diferentes.

    A trigger usa ST_Overlaps. A validação local é deliberadamente mais
    conservadora porque o PostGIS encontrou sobreposições que o método
    QgsGeometry.overlaps não reportou. Toda sobreposição também é uma
    interseção que não apenas toca a borda.
    """
    bbox_teste = geometria_teste.boundingBox()

    for id_outra, dados_outra in geometrias_finais.items():
        geometria_outra = dados_outra['geom']
        sq_outra = dados_outra['sq']

        if considerar_sq_diferente:
            # No PostgreSQL, NULL <> valor (e NULL <> NULL) não resulta em
            # TRUE. Portanto, nesses casos a linha também não entra no IF
            # EXISTS.
            if sq_teste in (None, NULL) or sq_outra in (None, NULL):
                continue

            # A trigger não exclui pelo ID: ela exclui pelo SQ.
            if sq_outra == sq_teste:
                continue

        if not bbox_teste.intersects(geometria_outra.boundingBox()):
            continue

        # Geometrias que somente tocam suas bordas continuam permitidas.
        # O teste é mais restritivo que ST_Overlaps e, portanto, também
        # bloqueia contenção ou igualdade entre SQs diferentes.
        if geometria_teste.intersects(geometria_outra) and not geometria_teste.touches(geometria_outra):
            return id_outra

    return None

def criar_contexto_postgis(layer_quadras):
    """Cria uma conexão somente de consulta usando a URI da própria camada."""
    if layer_quadras.providerType() != 'postgres':
        return None, 'A camada de quadras não usa o provedor PostgreSQL.'

    try:
        provedor = layer_quadras.dataProvider()
        uri = provedor.uri()
        metadados = QgsProviderRegistry.instance().providerMetadata('postgres')
        conexao = metadados.createConnection(uri.uri(), {})
        conexao.executeSql('SELECT 1')

        esquema = uri.schema()
        tabela = uri.table()
        coluna_geom = uri.geometryColumn() or 'geom'
        srid = layer_quadras.crs().postgisSrid()

        if not tabela or srid <= 0:
            return None, 'Não foi possível identificar a tabela ou o SRID das quadras.'

        return {
            'conexao': conexao,
            'esquema': esquema,
            'tabela': tabela,
            'coluna_geom': coluna_geom,
            'srid': srid
        }, None
    except Exception as erro:
        return None, str(erro)

def citar_identificador_postgres(nome):
    """Protege nomes de esquema, tabela e coluna usados no SELECT."""
    return '"' + str(nome).replace('"', '""') + '"'

def encontrar_colisao_no_postgis(
    contexto,
    geometria_teste,
    sq_teste,
    considerar_sq_diferente=True
):
    """Executa no servidor a mesma condição ST_Overlaps da trigger."""
    if considerar_sq_diferente and sq_teste in (None, NULL):
        return None, None

    try:
        hex_wkb = bytes(geometria_teste.asWkb()).hex()
        condicao_sq = ''
        if considerar_sq_diferente:
            sq_sql = str(sq_teste).replace("'", "''")
            condicao_sq = f'b."sq" <> \'{sq_sql}\' AND'
        esquema = contexto['esquema']
        tabela = citar_identificador_postgres(contexto['tabela'])
        if esquema:
            tabela = f'{citar_identificador_postgres(esquema)}.{tabela}'

        geom = citar_identificador_postgres(contexto['coluna_geom'])
        srid = int(contexto['srid'])
        sql = f"""
            WITH candidata AS (
                SELECT ST_SetSRID(
                    ST_GeomFromWKB(decode('{hex_wkb}', 'hex')),
                    {srid}
                ) AS geom
            )
            SELECT b."id"::text, b."sq"::text
            FROM {tabela} AS b
            CROSS JOIN candidata AS c
            WHERE {condicao_sq}
              b.{geom} && c.geom
              AND ST_Overlaps(b.{geom}, c.geom)
            LIMIT 1
        """
        linhas = contexto['conexao'].executeSql(sql)
        if linhas:
            return {
                'id': linhas[0][0],
                'sq': linhas[0][1]
            }, None
        return None, None
    except Exception as erro:
        return None, str(erro)

def encontrar_colisao_pendente_no_postgis(
    contexto,
    geometrias_pendentes,
    geometria_teste,
    sq_teste
):
    """Compara a candidata com as geometrias ainda não salvas no PostGIS."""
    if sq_teste in (None, NULL) or not geometrias_pendentes:
        return None, None

    try:
        bbox_teste = geometria_teste.boundingBox()
        valores = []
        srid = int(contexto['srid'])

        for id_pendente, dados_pendentes in geometrias_pendentes.items():
            sq_pendente = dados_pendentes['sq']
            geom_pendente = dados_pendentes['geom']

            if sq_pendente in (None, NULL) or sq_pendente == sq_teste:
                continue
            if not bbox_teste.intersects(geom_pendente.boundingBox()):
                continue

            id_sql = str(id_pendente).replace("'", "''")
            sq_sql = str(sq_pendente).replace("'", "''")
            hex_wkb = bytes(geom_pendente.asWkb()).hex()
            valores.append(
                f"('{id_sql}', '{sq_sql}', "
                f"ST_SetSRID(ST_GeomFromWKB(decode('{hex_wkb}', 'hex')), {srid}))"
            )

        if not valores:
            return None, None

        hex_candidata = bytes(geometria_teste.asWkb()).hex()
        sq_candidata = str(sq_teste).replace("'", "''")
        sql = f"""
            WITH candidata AS (
                SELECT ST_SetSRID(
                    ST_GeomFromWKB(decode('{hex_candidata}', 'hex')),
                    {srid}
                ) AS geom
            ),
            pendentes(id, sq, geom) AS (
                VALUES {', '.join(valores)}
            )
            SELECT p.id, p.sq
            FROM pendentes AS p
            CROSS JOIN candidata AS c
            WHERE p.sq <> '{sq_candidata}'
              AND p.geom && c.geom
              AND ST_Overlaps(p.geom, c.geom)
            LIMIT 1
        """
        linhas = contexto['conexao'].executeSql(sql)
        if linhas:
            return {
                'id': linhas[0][0],
                'sq': linhas[0][1]
            }, None
        return None, None
    except Exception as erro:
        return None, str(erro)

def extrair_lotes_todos_os_setores():
    iface.messageBar().pushMessage("Aguarde", "Verificando camadas no projeto...", level=Qgis.Info, duration=2)
    
    layer_edif = obter_camada_do_projeto(NOME_CAMADA_EDIF)
    layer_lotes = obter_camada_do_projeto(NOME_CAMADA_LOTES)
    layer_quadras = obter_camada_do_projeto(NOME_CAMADA_QUADRAS)
    layer_setores = obter_camada_do_projeto(NOME_CAMADA_SETORES)
    if not all([layer_edif, layer_lotes, layer_quadras, layer_setores]):
        iface.messageBar().pushMessage("Erro", "Não foi possível encontrar todas as camadas.", level=Qgis.Critical, duration=7)
        return

    campos_obrigatorios = {
        layer_quadras: ['sq'],
        layer_lotes: ['sq', 'cod_lf', 'sql'],
        layer_edif: ['sql'],
        layer_setores: ['cod_sf'],
    }

    if not validar_campos_obrigatorios(campos_obrigatorios):
        return

    camadas_geometricas = [layer_edif, layer_lotes, layer_quadras, layer_setores]
    crs_referencia = layer_edif.crs()

    if crs_referencia.isGeographic():
        iface.messageBar().pushMessage(
            'Erro',
            'A camada de edificações usa coordenadas geográficas. '
            'O buffer de 0,50 não pode ser interpretado como metros.',
            level=Qgis.Critical,
            duration=10
        )
        return

    if any(camada.crs() != crs_referencia for camada in camadas_geometricas):
        iface.messageBar().pushMessage(
            'Erro',
            'As camadas não possuem o mesmo SRC.',
            level=Qgis.Critical,
            duration=10
        )
        return

    setores = escolher_setores_para_processar(layer_setores)
    if setores is None:
        iface.messageBar().pushMessage("Erro", "Nenhum setor encontrado na camada.", level=Qgis.Critical, duration=5)
        return
    if not setores:
        iface.messageBar().pushMessage(
            'Cancelado',
            'Nenhum setor foi processado.',
            level=Qgis.Info,
            duration=5
        )
        return

    total_lotes_criados = 0
    total_edif_atualizadas = 0

    wkb_lotes = layer_lotes.wkbType()
    is_multi_lote = QgsWkbTypes.isMultiType(wkb_lotes)

    idx_q_sq = layer_quadras.fields().indexOf('sq')
    idx_l_sq, idx_l_sql, idx_l_lf = [layer_lotes.fields().indexOf(f) for f in ['sq', 'sql', 'cod_lf']]
    idx_e_sql = layer_edif.fields().indexOf('sql')

    contexto_postgis, erro_postgis = criar_contexto_postgis(layer_quadras)
    if erro_postgis is not None:
        iface.messageBar().pushMessage(
            'Erro',
            'Não foi possível preparar a validação de sobreposição no PostGIS. '
            'Nenhuma alteração foi realizada.',
            level=Qgis.Critical,
            duration=15
        )
        print(f'[ERRO VALIDAÇÃO POSTGIS] {erro_postgis}')
        return

    # Abre a edição das camadas uma única vez para todo o processo
    layer_lotes.startEditing()
    layer_edif.startEditing()
    layer_quadras.startEditing()

    # Mantém a geometria final prevista de todas as quadras. Isso evita
    # colisões entre alterações ainda pendentes de salvamento no QGIS.
    geometrias_finais_quadras = {
        feat.id(): {
            'geom': QgsGeometry(feat.geometry()),
            'sq': feat.attribute(idx_q_sq)
        }
        for feat in layer_quadras.getFeatures()
    }

    # Não acumula uma nova execução sobre quadras, lotes ou edificações
    # que já estavam pendentes no QGIS.
    pendencias_por_camada = {}
    for camada in (layer_quadras, layer_lotes, layer_edif):
        ids_pendentes = set()
        buffer_camada = camada.editBuffer()
        if buffer_camada is not None:
            ids_pendentes.update(buffer_camada.changedGeometries().keys())
            ids_pendentes.update(buffer_camada.changedAttributeValues().keys())
            ids_pendentes.update(buffer_camada.addedFeatures().keys())
            ids_pendentes.update(buffer_camada.deletedFeatureIds())
        if ids_pendentes:
            pendencias_por_camada[camada.name()] = len(ids_pendentes)

    if pendencias_por_camada:
        resumo_pendencias = ', '.join(
            f'{nome}: {quantidade}'
            for nome, quantidade in pendencias_por_camada.items()
        )
        iface.messageBar().pushMessage(
            'Erro',
            'Já existem alterações pendentes. Reverta as camadas indicadas '
            'no Console Python antes de executar novamente.',
            level=Qgis.Critical,
            duration=15
        )
        print(f'[BUFFER PENDENTE] {resumo_pendencias}')
        return

    wkb_quadra = layer_quadras.wkbType()
    is_multi_quadra = QgsWkbTypes.isMultiType(wkb_quadra)

    total_quadras_criadas = 0
    total_ampliacoes_bloqueadas = 0
    # Guarda somente as quadras que este script efetivamente alterou. O
    # resumo final permite confrontá-las com o erro devolvido pelo PostGIS.
    quadras_alteradas = {}
    geometrias_pendentes_postgis = {}

    # Loop para percorrer CADA SETOR encontrado
    for idx_setor, setor_selecionado in enumerate(setores):
        cod_sf_atual = setor_selecionado.attribute(NOME_CAMPO_SETOR)
        diagnostico(f"Processando Setor ({idx_setor + 1}/{len(setores)}) - Código: {cod_sf_atual}")

        geom_setor = setor_selecionado.geometry()
        if geom_setor.isEmpty():
            continue
            
        bbox_setor = geom_setor.boundingBox()

        engine_setor = QgsGeometry.createGeometryEngine(geom_setor.constGet())
        engine_setor.prepareGeometry()

        # Caches espaciais baseados na bbox do setor atual
        req_quadras = QgsFeatureRequest().setFilterRect(bbox_setor).setSubsetOfAttributes([idx_q_sq])
        quadras_in_bbox = {}
        mapa_quadras_por_sq = {} 
        index_quadras = QgsSpatialIndex()
        for feat in layer_quadras.getFeatures(req_quadras):
            quadras_in_bbox[feat.id()] = feat
            index_quadras.addFeature(feat)
            sq = feat.attribute(idx_q_sq)
            if sq not in (None, NULL):
                mapa_quadras_por_sq[str(sq).strip()] = feat

        atributos_lotes = [idx_l_sql, idx_l_lf]
        if idx_l_sq != -1:
            atributos_lotes.append(idx_l_sq)

        req_lotes = QgsFeatureRequest().setFilterRect(bbox_setor).setSubsetOfAttributes(atributos_lotes)
        lotes_in_bbox = {}
        index_lotes = QgsSpatialIndex()
        max_lf_dict = {}
        for feat in layer_lotes.getFeatures(req_lotes):
            lotes_in_bbox[feat.id()] = feat
            index_lotes.addFeature(feat)
            sq_val = obter_sq_do_lote(feat, idx_l_sq, idx_l_sql)
            lf_val = feat.attribute(idx_l_lf)
            if sq_val not in (None, NULL) and lf_val not in (None, NULL):
                try:
                    lf_int = int(lf_val)
                    sq_val = str(sq_val).strip()
                    if sq_val not in max_lf_dict or lf_int > max_lf_dict[sq_val]:
                        max_lf_dict[sq_val] = lf_int
                except (TypeError, ValueError):
                    pass

        req_edif = QgsFeatureRequest().setFilterRect(bbox_setor).setSubsetOfAttributes([idx_e_sql])
        edificacoes_in_bbox = {}
        index_edif = QgsSpatialIndex()
        for feat in layer_edif.getFeatures(req_edif):
            edificacoes_in_bbox[feat.id()] = feat
            index_edif.addFeature(feat)

        ids_edificacoes_no_setor = index_edif.intersects(bbox_setor)
        
        edificacoes_por_quadra = {}
        mapa_edificacoes_para_atualizar = {}
        cache_quadras_validas = {}
        
        # Regras de Negócio Espaciais por Setor
        for id_edif in ids_edificacoes_no_setor:
            feat_edif = edificacoes_in_bbox[id_edif]
            
            val_sql = feat_edif.attribute(idx_e_sql)
            if val_sql not in (None, NULL) and str(val_sql).strip() != '':
                continue
                
            geom_edif = feat_edif.geometry()
            if not geom_edif.isGeosValid():
                continue
            
            engine_edif = QgsGeometry.createGeometryEngine(geom_edif.constGet())
            engine_edif.prepareGeometry()

            # O setor deve conter 100% da edificação. Se vazar para fora, ignora.
            if not engine_setor.contains(geom_edif.constGet()):
                diagnostico(f"Edificação ID {feat_edif.id()} ignorada: Cruza a divisa do setor {cod_sf_atual}.")
                continue
                
            bbox_edif = geom_edif.boundingBox()

            # Edificação vs Edificação
            sobrepoe_outra_edificacao = False
            for id_outra in index_edif.intersects(bbox_edif):
                if id_outra == feat_edif.id(): 
                    continue
                feat_outra = edificacoes_in_bbox[id_outra]
                geom_outra = feat_outra.geometry().constGet()
                if engine_edif.intersects(geom_outra) and not engine_edif.touches(geom_outra):
                    sobrepoe_outra_edificacao = True
                    break
                        
            if sobrepoe_outra_edificacao:
                continue

            # Lotes
            sobrepoe_lote_invalido = False
            lotes_adjacentes = []
            lote_pai = None 
            
            for id_l in index_lotes.intersects(bbox_edif):
                feat_lote_exist = lotes_in_bbox[id_l]
                geom_lote_exist = feat_lote_exist.geometry().constGet()
                
                if engine_edif.within(geom_lote_exist):
                    lote_pai = feat_lote_exist
                    break
                elif engine_edif.intersects(geom_lote_exist):
                    if not engine_edif.touches(geom_lote_exist):
                        sobrepoe_lote_invalido = True
                        break
                    else:
                        lotes_adjacentes.append(feat_lote_exist)
            
            if lote_pai:
                sql_herdado = lote_pai.attribute(idx_l_sql)
                if sql_herdado not in (None, NULL) and idx_e_sql != -1:
                    mapa_edificacoes_para_atualizar[feat_edif.id()] = {idx_e_sql: sql_herdado}
                continue

            if sobrepoe_lote_invalido: 
                continue

            # Quadras
            quadras_contendo = []
            quadras_intersectadas_parcialmente = []
            
            for id_q in index_quadras.intersects(bbox_edif):
                feat_q = quadras_in_bbox[id_q]
                geom_q = feat_q.geometry().constGet()
                
                if engine_edif.within(geom_q):
                    quadras_contendo.append(feat_q)
                elif engine_edif.intersects(geom_q):
                    quadras_intersectadas_parcialmente.append(feat_q)
            
            quadra_valida = None
            quadra_nova = False
            
            if len(quadras_contendo) == 1:
                quadra_valida = quadras_contendo[0]
            elif len(quadras_contendo) == 0 and len(lotes_adjacentes) > 0:
                for lote_adj in lotes_adjacentes:
                    sq_lote = obter_sq_do_lote(lote_adj, idx_l_sq, idx_l_sql)
                    if sq_lote not in (None, NULL):
                        quadra_valida = mapa_quadras_por_sq.get(str(sq_lote).strip())
                    if quadra_valida:
                        break
            
            # 1. Nova Regra: Se a edificação invadir a rua ou tocar a borda de APENAS UMA quadra
            if not quadra_valida and len(quadras_intersectadas_parcialmente) == 1:
                quadra_valida = quadras_intersectadas_parcialmente[0]

            tem_relacao_com_lote = (
                lote_pai is not None
                or len(lotes_adjacentes) > 0
                or sobrepoe_lote_invalido
            )
            tem_relacao_com_quadra = (
                len(quadras_contendo) > 0
                or len(quadras_intersectadas_parcialmente) > 0
            )
            edificacao_isolada = (
                not tem_relacao_com_lote
                and not tem_relacao_com_quadra
            )

            # Uma nova quadra só pode ser criada para uma edificação
            # sem qualquer relação espacial com lote ou quadra existentes.
            if not quadra_valida and edificacao_isolada and CRIAR_QUADRA_PARA_EDIFICACAO_ISOLADA:
                geometria_nova_quadra = geom_edif.buffer(MARGEM_NOVA_QUADRA, 5) if MARGEM_NOVA_QUADRA > 0 else QgsGeometry(geom_edif)
                geometria_nova_quadra = ajustar_geometria_para_camada(geometria_nova_quadra, is_multi_quadra)

                if geometria_nova_quadra.isEmpty() or not geometria_nova_quadra.isGeosValid():
                    diagnostico(f'Edificação ID {feat_edif.id()} ignorada: Não foi possível gerar a nova quadra.')
                    continue

                if not engine_setor.contains(geometria_nova_quadra.constGet()):
                    diagnostico(
                        f'Edificação {feat_edif.id()} ignorada: '
                        'a nova quadra ultrapassaria o setor.'
                    )
                    continue

                id_quadras_colidida = encontrar_colisao_com_quadra(
                    geometrias_finais_quadras,
                    geometria_nova_quadra,
                    None,
                    considerar_sq_diferente=False
                )
                if id_quadras_colidida is not None:
                    sq_colidida = geometrias_finais_quadras[id_quadras_colidida]['sq']
                    print(
                        f'[SOBREPOSIÇÃO LOCAL] Edificação ID {feat_edif.id()}: '
                        f'a nova quadra sobreporia a quadra '
                        f'SQ {sq_colidida} (ID {id_quadras_colidida}).'
                    )
                    continue

                conflito_postgis, erro_postgis = encontrar_colisao_no_postgis(
                    contexto_postgis,
                    geometria_nova_quadra,
                    None,
                    considerar_sq_diferente=False
                )
                if erro_postgis is not None:
                    print(f'[ERRO VALIDAÇÃO POSTGIS] {erro_postgis}')
                    iface.messageBar().pushMessage(
                        'Erro',
                        'A consulta de sobreposição ao PostGIS falhou. '
                        'Descarte as alterações desta execução.',
                        level=Qgis.Critical,
                        duration=15
                    )
                    return
                if conflito_postgis is not None:
                    print(
                        f'[SOBREPOSIÇÃO POSTGIS] Edificação ID {feat_edif.id()}: '
                        f'a nova quadra sobreporia a quadra '
                        f'SQ {conflito_postgis["sq"]} (ID {conflito_postgis["id"]}).'
                    )
                    continue

                nova_feat_quadra = QgsFeature(layer_quadras.fields())
                nova_feat_quadra.setGeometry(geometria_nova_quadra)

                if not layer_quadras.addFeature(nova_feat_quadra):
                    diagnostico(f'Edificação ID {feat_edif.id()} ignorada: Falha ao inserir a nova quadra.')
                    continue

                q_id_novo = nova_feat_quadra.id()
                geometrias_finais_quadras[q_id_novo] = {
                    'geom': QgsGeometry(geometria_nova_quadra),
                    'sq': None
                }
                total_quadras_criadas += 1
                quadras_alteradas[q_id_novo] = '(definido pela trigger ao salvar)'
                geometrias_pendentes_postgis[q_id_novo] = {
                    'geom': QgsGeometry(geometria_nova_quadra),
                    'sq': None
                }

                # Não adiciona a nova quadra ao índice de classificação neste
                # setor. Assim, duas edificações isoladas geram duas quadras,
                # em vez de a segunda ser absorvida pela primeira.
                cache_quadras_validas[q_id_novo] = True

                # A trigger só informa os códigos ao PostGIS durante o
                # salvamento. Após salvar e recarregar as quadras, uma nova
                # execução criará o lote e atualizará esta edificação.
                continue

            # Se há mais de uma quadra contendo a edificação ou mais de uma
            # quadra tocada, a situação é ambígua e não deve gerar uma quadra.
            if not quadra_valida and not edificacao_isolada:
                diagnostico(f'Edificação ID {feat_edif.id()} ignorada: Relação ambígua com as quadras existentes.')
                continue

            # Diagnóstico 1: Se mesmo assim não achou quadra
            if not quadra_valida:
                diagnostico(f"Edificação ID {feat_edif.id()} ignorada: Nenhuma quadra encontrada.")
                continue 

            q_id = quadra_valida.id()
            if q_id not in cache_quadras_validas:
                geom_q_valida_teste = quadra_valida.geometry()
                # CORREÇÃO CRÍTICA: 'intersects' em vez de 'contains' para evitar erros de borda
                cache_quadras_validas[q_id] = engine_setor.intersects(geom_q_valida_teste.constGet())
                
            # Diagnóstico 2: Quadra inválida
            if not cache_quadras_validas[q_id]:
                diagnostico(f"Edificação ID {feat_edif.id()} ignorada: A quadra {q_id} não pertence/intersecta o setor.")
                continue
            
            # Quadras existentes continuam sendo ampliadas para alcançar a
            # edificação. Quadras recém-criadas já foram geradas com a
            # geometria da edificação (mais a margem configurada).
            if not quadra_nova:
                geom_q_atual = quadra_valida.geometry()
                geom_edif_atual = feat_edif.geometry()

                # Não envie um UPDATE de geometria quando a quadra já contém
                # integralmente a edificação. Mesmo que combine() produza uma
                # geometria topologicamente igual, changeGeometry() marcaria a
                # feição como alterada e faria a trigger revalidar uma quadra
                # cuja forma não precisava ser modificada.
                if engine_edif.within(geom_q_atual.constGet()):
                    if q_id not in edificacoes_por_quadra:
                        edificacoes_por_quadra[q_id] = []
                    edificacoes_por_quadra[q_id].append(feat_edif)
                    continue

                # A edificação está fora da quadra existente. Não crie
                # lote para ela sem antes ampliar a quadra, pois a trigger de
                # lotes exige ST_Within(lote.geom, quadra.geom). Quando a
                # ampliação estiver desabilitada, ignore este caso com
                # segurança, sem colocar UPDATE geométrico no buffer.
                if not AMPLIAR_QUADRAS_EXISTENTES:
                    total_ampliacoes_bloqueadas += 1
                    continue

                distancia_vao = geom_q_atual.distance(geom_edif_atual)

                if distancia_vao > 0:
                    geom_para_unir = geom_edif_atual.buffer(distancia_vao + 0.001, 5)
                else:
                    geom_para_unir = geom_edif_atual

                nova_geom_quadra = geom_q_atual.combine(geom_para_unir)
                nova_geom_quadra = ajustar_geometria_para_camada(nova_geom_quadra, is_multi_quadra)

                if nova_geom_quadra.isEmpty() or not nova_geom_quadra.isGeosValid():
                    diagnostico(f'Edificação ID {feat_edif.id()} ignorada: expansão gerou geometria inválida.')
                    continue

                # Segunda proteção contra UPDATEs sem alteração espacial
                # efetiva (por exemplo, diferenças numéricas nas bordas).
                if nova_geom_quadra.equals(geom_q_atual):
                    if q_id not in edificacoes_por_quadra:
                        edificacoes_por_quadra[q_id] = []
                    edificacoes_por_quadra[q_id].append(feat_edif)
                    continue

                id_quadras_colidida = encontrar_colisao_com_quadra(
                    geometrias_finais_quadras,
                    nova_geom_quadra,
                    quadra_valida.attribute(idx_q_sq)
                )

                if id_quadras_colidida is not None:
                    sq_colidida = geometrias_finais_quadras[id_quadras_colidida]['sq']
                    print(
                        f'[SOBREPOSIÇÃO LOCAL] Edificação ID {feat_edif.id()}: '
                        f'a alteração da quadra SQ '
                        f'{quadra_valida.attribute(idx_q_sq)} seria recusada pela trigger: '
                        f'sobreposição com SQ {sq_colidida} (ID {id_quadras_colidida}).'
                    )
                    continue

                sq_quadra_atual = quadra_valida.attribute(idx_q_sq)
                conflito_postgis, erro_postgis = encontrar_colisao_no_postgis(
                    contexto_postgis,
                    nova_geom_quadra,
                    sq_quadra_atual
                )
                if erro_postgis is not None:
                    print(f'[ERRO VALIDAÇÃO POSTGIS] {erro_postgis}')
                    iface.messageBar().pushMessage(
                        'Erro',
                        'A consulta de sobreposição ao PostGIS falhou. '
                        'Descarte as alterações desta execução.',
                        level=Qgis.Critical,
                        duration=15
                    )
                    return
                if conflito_postgis is not None:
                    print(
                        f'[SOBREPOSIÇÃO POSTGIS] Edificação ID {feat_edif.id()}: '
                        f'a alteração da quadra SQ {sq_quadra_atual} seria '
                        f'recusada pela trigger: sobreposição com SQ '
                        f'{conflito_postgis["sq"]} (ID {conflito_postgis["id"]}).'
                    )
                    continue

                conflito_pendente, erro_postgis = encontrar_colisao_pendente_no_postgis(
                    contexto_postgis,
                    geometrias_pendentes_postgis,
                    nova_geom_quadra,
                    sq_quadra_atual
                )
                if erro_postgis is not None:
                    print(f'[ERRO VALIDAÇÃO POSTGIS] {erro_postgis}')
                    iface.messageBar().pushMessage(
                        'Erro',
                        'A comparação das quadras pendentes no PostGIS falhou. '
                        'Descarte as alterações desta execução.',
                        level=Qgis.Critical,
                        duration=15
                    )
                    return
                if conflito_pendente is not None:
                    print(
                        f'[SOBREPOSIÇÃO PENDENTE POSTGIS] Edificação ID '
                        f'{feat_edif.id()}: a alteração da quadra SQ '
                        f'{sq_quadra_atual} sobreporia a quadra pendente SQ '
                        f'{conflito_pendente["sq"]} (ID {conflito_pendente["id"]}).'
                    )
                    continue

                if not layer_quadras.changeGeometry(q_id, nova_geom_quadra):
                    diagnostico(f'Edificação ID {feat_edif.id()} ignorada: falha ao alterar a geometria da quadra {q_id}.')
                    continue

                index_quadras.deleteFeature(quadra_valida)
                quadra_valida.setGeometry(nova_geom_quadra)
                index_quadras.addFeature(quadra_valida)
                geometrias_finais_quadras[q_id] = {
                    'geom': QgsGeometry(nova_geom_quadra),
                    'sq': quadra_valida.attribute(idx_q_sq)
                }
                quadras_alteradas[q_id] = quadra_valida.attribute(idx_q_sq)
                geometrias_pendentes_postgis[q_id] = {
                    'geom': QgsGeometry(nova_geom_quadra),
                    'sq': quadra_valida.attribute(idx_q_sq)
                }

            if q_id not in edificacoes_por_quadra:
                edificacoes_por_quadra[q_id] = []
            edificacoes_por_quadra[q_id].append(feat_edif)

        # Criação de Lotes para o setor atual
        novas_features_lote = []
        atualizacoes_edificacoes_de_novos_lotes = []
        for q_id, lista_edif in edificacoes_por_quadra.items():
            quadra = quadras_in_bbox[q_id]
            str_sq = str(quadra.attribute(idx_q_sq)).strip() if quadra.attribute(idx_q_sq) not in (None, NULL) else ""
            if len(str_sq) != 7:
                diagnostico(f'Quadra ID {q_id}: SQ inválido ({str_sq}).')
                continue

            cod_lf_atual = max_lf_dict.get(str_sq, 0) + 1
            for feat_edif in lista_edif: 
                if cod_lf_atual > 9999:
                    diagnostico(f'Quadra {str_sq}: limite de lotes atingido.')
                    break

                str_lf_atual = str(cod_lf_atual).zfill(4)
                str_sql_atual = f"{str_sq}{str_lf_atual}"

                if len(str_sql_atual) != 11:
                    diagnostico(f'SQL inválido: {str_sql_atual}')
                    cod_lf_atual += 1
                    continue

                # O SQ do lote é confirmado pela trigger. O script gera
                # manualmente cod_lf e SQL a partir do SQ da quadra escolhida.
                geom_lote = QgsGeometry(feat_edif.geometry())
                if is_multi_lote and not geom_lote.isMultipart():
                    geom_lote.convertToMultiType()
                elif not is_multi_lote and geom_lote.isMultipart():
                    maior_area_l = -1
                    geom_simples_l = geom_lote
                    for part in geom_lote.asGeometryCollection():
                        if part.area() > maior_area_l:
                            maior_area_l = part.area()
                            geom_simples_l = part
                    geom_lote = geom_simples_l

                if geom_lote.isEmpty() or not geom_lote.isGeosValid():
                    diagnostico(
                        f'Edificação ID {feat_edif.id()} ignorada: '
                        'a geometria do lote seria inválida.'
                    )
                    continue

                nova_feat_lote = QgsFeature(layer_lotes.fields())
                nova_feat_lote.setGeometry(geom_lote)

                # A trigger atualizar_sql_lote confirma o SQ espacial. Como
                # cod_lf não é nulo, ela preserva a numeração e o SQL
                # calculados pelo script.
                if idx_l_lf != -1:
                    nova_feat_lote[idx_l_lf] = cod_lf_atual
                if idx_l_sql != -1:
                    nova_feat_lote[idx_l_sql] = str_sql_atual

                novas_features_lote.append(nova_feat_lote)
                atualizacoes_edificacoes_de_novos_lotes.append(
                    (feat_edif.id(), str_sql_atual)
                )
                cod_lf_atual += 1

        # Acumula as inserções/atualizações na sessão de edição aberta
        if novas_features_lote:
            if layer_lotes.addFeatures(novas_features_lote):
                total_lotes_criados += len(novas_features_lote)
                for fid_edif, sql_edif in atualizacoes_edificacoes_de_novos_lotes:
                    mapa_edificacoes_para_atualizar[fid_edif] = {
                        idx_e_sql: sql_edif
                    }
            else:
                diagnostico('Não foi possível adicionar os novos lotes ao buffer do QGIS.')

        if mapa_edificacoes_para_atualizar:
            for fid, atributos in mapa_edificacoes_para_atualizar.items():
                feat_update = layer_edif.getFeature(fid)
                if feat_update.isValid():
                    for idx_campo, novo_valor in atributos.items():
                        feat_update.setAttribute(idx_campo, novo_valor)
                    
                    sucesso = layer_edif.updateFeature(feat_update)
                    if not sucesso:
                        valores_debug = [f"Valor: '{v}' (Tamanho: {len(str(v))})" for v in atributos.values()]
                        diagnostico(f"ERRO: O QGIS bloqueou a atualização da Edificação {fid}. {valores_debug}")
                        
            total_edif_atualizadas += len(mapa_edificacoes_para_atualizar)

    # Atualiza a tela ao finalizar todos os setores
    layer_lotes.triggerRepaint()
    layer_edif.triggerRepaint()
    layer_quadras.triggerRepaint()

    if quadras_alteradas:
        lista_quadras_alteradas = ', '.join(
            f'ID {q_id} / SQ {sq}'
            for q_id, sq in sorted(quadras_alteradas.items())
        )
        filtro_quadras = layer_quadras.subsetString() or '(nenhum)'
        print(
            '[RESUMO POSTGIS] '
            f'Filtro da camada: {filtro_quadras}. '
            f'Quadras alteradas ({len(quadras_alteradas)}): '
            f'{lista_quadras_alteradas}'
        )

    if total_ampliacoes_bloqueadas:
        print(
            '[AMPLIAÇÕES BLOQUEADAS] '
            f'{total_ampliacoes_bloqueadas} edificação(ões) ficaram sem lote '
            'porque exigiriam alteração de geometria em quadra existente.'
        )

    if total_lotes_criados > 0 or total_edif_atualizadas > 0 or total_quadras_criadas > 0:
        orientacao_salvamento = (
            'Salve quadras e recarregue a camada. Execute o script novamente '
            'para gerar os lotes e atualizar as edificações das novas quadras.'
            if total_quadras_criadas > 0 else
            'Salve lotes e, por fim, edificações.'
        )
        iface.messageBar().pushMessage(
            "Sucesso Geral",
            f"Processo concluído para todos os setores! {total_quadras_criadas} Quadra(s) e {total_lotes_criados} Lote(s) criado(s), além de {total_edif_atualizadas} Edificação(ões) atualizada(s). {orientacao_salvamento}",
            level=Qgis.Success,
            duration=10
        )
    else:
        iface.messageBar().pushMessage("Concluído", "Nenhuma edificação atendeu aos critérios nos setores analisados.", level=Qgis.Info, duration=5)

extrair_lotes_todos_os_setores()
