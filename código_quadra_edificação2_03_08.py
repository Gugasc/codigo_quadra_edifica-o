from qgis.core import (
    QgsProject,
    QgsFeature,
    QgsFeatureRequest,
    QgsSpatialIndex,
    QgsGeometry,
    Qgis,
    QgsWkbTypes,
    NULL,
    QgsGeometryEngine
)
from qgis.utils import iface

# ==========================================
# 1. NOMES DAS CAMADAS NO PROJETO DO QGIS
# ==========================================
NOME_CAMADA_EDIF = 'ct_edificacao_fiscal'
NOME_CAMADA_LOTES = 'ct_lote_fiscal'
NOME_CAMADA_QUADRAS = 'ct_quadra_fiscal'
NOME_CAMADA_SETORES = 'ct_setor_fiscal'

NOME_CAMPO_SETOR = 'cod_sf'
NOME_CAMPO_SETOR_SAT = 'cod_sf_sat'

# Se cod_sf_sat não estiver na camada de setores, informe aqui o nome
# da camada/tabela de relacionamento entre cod_sf e cod_sf_sat.
# Deixe None quando o campo estiver diretamente na camada de setores.
NOME_CAMADA_RELACAO_SF_SAT = None
NOME_CAMPO_RELACAO_SF = 'cod_sf'
NOME_CAMPO_RELACAO_SAT = 'cod_sf_sat'

# ================================================================
# CONFIGURAÇÃO DAS QUADRAS CRIADAS PARA EDIFICAÇÕES ISOLADAS
# ================================================================
# A margem é aplicada ao redor da edificação para que a nova quadra
# fique ligeiramente maior que o lote. Use 0.0 para a quadra ter a
# mesma geometria da edificação. O valor está na unidade do projeto
# (normalmente metros em uma camada UTM).
MARGEM_NOVA_QUADRA = 0.00001

# O SQ é formado por cod_sf_sat (3 dígitos) + cod_qf (4 dígitos).
# Exemplo: cod_sf_sat=201 e cod_qf=134 => sq=2010134.
CRIAR_QUADRA_PARA_EDIFICACAO_ISOLADA = True

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

def carregar_mapa_sf_para_sat(layer_relacao):
    mapa_sf_para_sat = {}

    if layer_relacao is None:
        return mapa_sf_para_sat

    idx_relacao_sf = layer_relacao.fields().indexOf(NOME_CAMPO_RELACAO_SF)
    idx_relacao_sat = layer_relacao.fields().indexOf(NOME_CAMPO_RELACAO_SAT)

    for feat_relacao in layer_relacao.getFeatures():
        valor_sf = feat_relacao.attribute(idx_relacao_sf)
        valor_sat = feat_relacao.attribute(idx_relacao_sat)

        if valor_sf in (None, NULL) or valor_sat in (None, NULL):
            continue

        chave_sf = str(valor_sf).strip()
        if chave_sf:
            try:
                mapa_sf_para_sat[chave_sf] = int(valor_sat)
            except (TypeError, ValueError):
                continue

    return mapa_sf_para_sat

def obter_cod_sf_sat_do_setor(setor, indice_setor_sat, mapa_sf_para_sat):
    if indice_setor_sat != -1:
        valor_sat = setor.attribute(indice_setor_sat)
        if valor_sat not in (None, NULL):
            try:
                return int(valor_sat)
            except (TypeError, ValueError):
                return None

    valor_sf = setor.attribute(NOME_CAMPO_SETOR)
    chave_sf = '' if valor_sf in (None, NULL) else str(valor_sf).strip()
    return mapa_sf_para_sat.get(chave_sf)

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

def obter_proximo_cod_qf(layer_quadras, indice_cod_sf, indice_cod_qf, cod_sf):
    """Obtém o próximo cod_qf dentro do cod_sf informado."""
    maior_cod_qf = 0

    if indice_cod_sf == -1 or indice_cod_qf == -1:
        return None

    try:
        cod_sf_num = int(cod_sf)
    except (TypeError, ValueError):
        return None

    for feat in layer_quadras.getFeatures():
        valor_sf = feat.attribute(indice_cod_sf)
        valor_qf = feat.attribute(indice_cod_qf)

        if valor_sf in (None, NULL) or valor_qf in (None, NULL):
            continue

        try:
            if int(valor_sf) == cod_sf_num:
                maior_cod_qf = max(maior_cod_qf, int(valor_qf))
        except (TypeError, ValueError):
            continue

    proximo_cod_qf = maior_cod_qf + 1
    return proximo_cod_qf if proximo_cod_qf <= 9999 else None

def montar_sq(cod_sf_sat, cod_qf):
    """Monta o SQ no formato fixo: 3 dígitos + 4 dígitos."""
    cod_sf_sat_num = int(cod_sf_sat)
    cod_qf_num = int(cod_qf)

    if not 0 <= cod_sf_sat_num <= 999 or not 0 <= cod_qf_num <= 9999:
        return None

    # O SQ deve sempre ter 7 caracteres:
    # cod_sf_sat com 3 posições + cod_qf com 4 posições.
    cod_sf_sat_formatado = f'{cod_sf_sat_num:03d}'
    cod_qf_formatado = f'{cod_qf_num:04d}'
    sq = f'{cod_sf_sat_formatado}{cod_qf_formatado}'

    return sq if len(sq) == 7 else None

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

def encontrar_colisao_com_quadra(layer_quadras, geometria_teste, id_ignorar=None):
    """Retorna o ID de uma quadra invadida ou None quando não há colisão."""
    requisicao = QgsFeatureRequest().setFilterRect(geometria_teste.boundingBox())

    for outra_quadra in layer_quadras.getFeatures(requisicao):
        if id_ignorar is not None and outra_quadra.id() == id_ignorar:
            continue

        geometria_outra = QgsGeometry(outra_quadra.geometry())
        if geometria_teste.intersects(geometria_outra) and not geometria_teste.touches(geometria_outra):
            return outra_quadra.id()

    return None

def extrair_lotes_todos_os_setores():
    iface.messageBar().pushMessage("Aguarde", "Verificando camadas no projeto...", level=Qgis.Info, duration=2)
    
    layer_edif = obter_camada_do_projeto(NOME_CAMADA_EDIF)
    layer_lotes = obter_camada_do_projeto(NOME_CAMADA_LOTES)
    layer_quadras = obter_camada_do_projeto(NOME_CAMADA_QUADRAS)
    layer_setores = obter_camada_do_projeto(NOME_CAMADA_SETORES)
    layer_relacao = (
        obter_camada_do_projeto(NOME_CAMADA_RELACAO_SF_SAT)
        if NOME_CAMADA_RELACAO_SF_SAT
        else None
    )
    
    if not all([layer_edif, layer_lotes, layer_quadras, layer_setores]):
        iface.messageBar().pushMessage("Erro", "Não foi possível encontrar todas as camadas.", level=Qgis.Critical, duration=7)
        return

    if NOME_CAMADA_RELACAO_SF_SAT and layer_relacao is None:
        iface.messageBar().pushMessage(
            'Erro',
            f'Não foi possível encontrar a camada de relação {NOME_CAMADA_RELACAO_SF_SAT}.',
            level=Qgis.Critical,
            duration=10
        )
        return

    campos_obrigatorios = {
        layer_quadras: ['sq', 'cod_sf', 'cod_sf_sat', 'cod_qf'],
        layer_lotes: ['sq', 'cod_lf', 'sql'],
        layer_edif: ['sql'],
        layer_setores: ['cod_sf'],
    }

    if not validar_campos_obrigatorios(campos_obrigatorios):
        return

    if layer_relacao is not None and not validar_campos_obrigatorios({
        layer_relacao: [NOME_CAMPO_RELACAO_SF, NOME_CAMPO_RELACAO_SAT]
    }):
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

    indice_setor_sat = layer_setores.fields().indexOf(NOME_CAMPO_SETOR_SAT)
    if indice_setor_sat == -1 and layer_relacao is None:
        iface.messageBar().pushMessage(
            'Erro',
            f'O campo {NOME_CAMPO_SETOR_SAT} não existe na camada de setores '
            'e nenhuma camada de relação foi configurada.',
            level=Qgis.Critical,
            duration=10
        )
        return

    mapa_sf_para_sat = carregar_mapa_sf_para_sat(layer_relacao)

    # Pega todos os setores da camada
    setores = list(layer_setores.getFeatures())
    if not setores:
        iface.messageBar().pushMessage("Erro", "Nenhum setor encontrado na camada.", level=Qgis.Critical, duration=5)
        return

    total_lotes_criados = 0
    total_edif_atualizadas = 0

    wkb_lotes = layer_lotes.wkbType()
    is_multi_lote = QgsWkbTypes.isMultiType(wkb_lotes)

    idx_q_sq, idx_q_sf, idx_q_sat, idx_q_qf = [layer_quadras.fields().indexOf(f) for f in ['sq', 'cod_sf', 'cod_sf_sat', 'cod_qf']]
    idx_l_sq, idx_l_sql, idx_l_lf = [layer_lotes.fields().indexOf(f) for f in ['sq', 'sql', 'cod_lf']]
    idx_e_sql = layer_edif.fields().indexOf('sql')

    # Abre a edição das camadas uma única vez para todo o processo
    layer_lotes.startEditing()
    layer_edif.startEditing()
    layer_quadras.startEditing()

    wkb_quadra = layer_quadras.wkbType()
    is_multi_quadra = QgsWkbTypes.isMultiType(wkb_quadra)

    total_quadras_criadas = 0

    # Loop para percorrer CADA SETOR encontrado
    for idx_setor, setor_selecionado in enumerate(setores):
        cod_sf_atual = setor_selecionado.attribute(NOME_CAMPO_SETOR)
        try:
            cod_sf_setor = int(cod_sf_atual)
        except (TypeError, ValueError):
            cod_sf_setor = None

        cod_sf_sat_setor = obter_cod_sf_sat_do_setor(
            setor_selecionado,
            indice_setor_sat,
            mapa_sf_para_sat
        )
        print(f"Processando Setor ({idx_setor + 1}/{len(setores)}) - Código: {cod_sf_atual}")

        if cod_sf_sat_setor is None:
            print(
                f'Setor {cod_sf_atual} sem relação válida com cod_sf_sat. '
                'Edificações isoladas deste setor não gerarão nova quadra.'
            )

        geom_setor = setor_selecionado.geometry()
        if geom_setor.isEmpty():
            continue
            
        bbox_setor = geom_setor.boundingBox()

        engine_setor = QgsGeometry.createGeometryEngine(geom_setor.constGet())
        engine_setor.prepareGeometry()

        # Caches espaciais baseados na bbox do setor atual
        req_quadras = QgsFeatureRequest().setFilterRect(bbox_setor).setSubsetOfAttributes([idx_q_sq, idx_q_sf, idx_q_sat, idx_q_qf])
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
                print(f"Edificação ID {feat_edif.id()} ignorada: Cruza a divisa do setor {cod_sf_atual}.")
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

            # Uma nova quadra/SQ só pode ser criada para uma edificação
            # sem qualquer relação espacial com lote ou quadra existentes.
            if not quadra_valida and edificacao_isolada and CRIAR_QUADRA_PARA_EDIFICACAO_ISOLADA:
                cod_sf_novo = cod_sf_setor
                cod_sf_sat_novo = cod_sf_sat_setor

                if cod_sf_novo is None or not 1 <= cod_sf_novo <= 114:
                    print(
                        f'Edificação ID {feat_edif.id()} ignorada: '
                        f'cod_sf inválido para o setor {cod_sf_atual}.'
                    )
                    continue

                if cod_sf_sat_novo is None or not 88 <= cod_sf_sat_novo <= 201:
                    print(
                        f'Edificação ID {feat_edif.id()} ignorada: '
                        f'cod_sf_sat inválido para o setor {cod_sf_atual}.'
                    )
                    continue

                cod_qf_novo = obter_proximo_cod_qf(
                    layer_quadras,
                    idx_q_sf,
                    idx_q_qf,
                    cod_sf_novo
                )

                if cod_qf_novo is None:
                    print(
                        f'Edificação ID {feat_edif.id()} ignorada: '
                        f'não foi possível gerar cod_qf para cod_sf {cod_sf_novo}.'
                    )
                    continue

                str_sq_nova = montar_sq(cod_sf_sat_novo, cod_qf_novo)
                if str_sq_nova is None or len(str_sq_nova) != 7:
                    print(
                        f'Edificação ID {feat_edif.id()} ignorada: '
                        f'SQ inválido gerado ({str_sq_nova}).'
                    )
                    continue

                geometria_nova_quadra = geom_edif.buffer(MARGEM_NOVA_QUADRA, 5) if MARGEM_NOVA_QUADRA > 0 else QgsGeometry(geom_edif)
                geometria_nova_quadra = ajustar_geometria_para_camada(geometria_nova_quadra, is_multi_quadra)

                if geometria_nova_quadra.isEmpty() or not geometria_nova_quadra.isGeosValid():
                    print(f'Edificação ID {feat_edif.id()} ignorada: Não foi possível gerar a nova quadra.')
                    continue

                if not engine_setor.contains(geometria_nova_quadra.constGet()):
                    print(
                        f'Edificação {feat_edif.id()} ignorada: '
                        'a nova quadra ultrapassaria o setor.'
                    )
                    continue

                id_quadras_colidida = encontrar_colisao_com_quadra(layer_quadras, geometria_nova_quadra)
                if id_quadras_colidida is not None:
                    print(
                        f'Edificação ID {feat_edif.id()} ignorada: '
                        f'a nova quadra colidiria com a quadra ID {id_quadras_colidida}.'
                    )
                    continue

                nova_feat_quadra = QgsFeature(layer_quadras.fields())
                nova_feat_quadra.setGeometry(geometria_nova_quadra)

                if idx_q_sq != -1:
                    nova_feat_quadra[idx_q_sq] = str_sq_nova
                if idx_q_sf != -1:
                    nova_feat_quadra[idx_q_sf] = cod_sf_novo
                if idx_q_sat != -1:
                    nova_feat_quadra[idx_q_sat] = cod_sf_sat_novo
                if idx_q_qf != -1:
                    nova_feat_quadra[idx_q_qf] = cod_qf_novo
                if not layer_quadras.addFeature(nova_feat_quadra):
                    print(f'Edificação ID {feat_edif.id()} ignorada: Falha ao inserir a nova quadra.')
                    continue

                q_id_novo = nova_feat_quadra.id()
                quadras_in_bbox[q_id_novo] = nova_feat_quadra
                quadra_valida = nova_feat_quadra
                quadra_nova = True
                total_quadras_criadas += 1

                # Não adiciona a nova quadra ao índice de classificação neste
                # setor. Assim, duas edificações isoladas geram duas quadras,
                # em vez de a segunda ser absorvida pela primeira.
                cache_quadras_validas[q_id_novo] = True

            # Se há mais de uma quadra contendo a edificação ou mais de uma
            # quadra tocada, a situação é ambígua e não deve gerar uma quadra.
            if not quadra_valida and not edificacao_isolada:
                print(f'Edificação ID {feat_edif.id()} ignorada: Relação ambígua com as quadras existentes.')
                continue

            # Diagnóstico 1: Se mesmo assim não achou quadra
            if not quadra_valida:
                print(f"Edificação ID {feat_edif.id()} ignorada: Nenhuma quadra encontrada.")
                continue 

            q_id = quadra_valida.id()
            if q_id not in cache_quadras_validas:
                geom_q_valida_teste = quadra_valida.geometry()
                # CORREÇÃO CRÍTICA: 'intersects' em vez de 'contains' para evitar erros de borda
                cache_quadras_validas[q_id] = engine_setor.intersects(geom_q_valida_teste.constGet())
                
            # Diagnóstico 2: Quadra inválida
            if not cache_quadras_validas[q_id]:
                print(f"Edificação ID {feat_edif.id()} ignorada: A quadra {q_id} não pertence/intersecta o setor.")
                continue
            
            # Quadras existentes continuam sendo ampliadas para alcançar a
            # edificação. Quadras recém-criadas já foram geradas com a
            # geometria da edificação (mais a margem configurada).
            if not quadra_nova:
                geom_q_atual = quadra_valida.geometry()
                geom_edif_atual = feat_edif.geometry()
                distancia_vao = geom_q_atual.distance(geom_edif_atual)

                if distancia_vao > 0:
                    geom_para_unir = geom_edif_atual.buffer(distancia_vao + 0.001, 5)
                else:
                    geom_para_unir = geom_edif_atual

                nova_geom_quadra = geom_q_atual.combine(geom_para_unir)
                nova_geom_quadra = ajustar_geometria_para_camada(nova_geom_quadra, is_multi_quadra)

                id_quadras_colidida = encontrar_colisao_com_quadra(
                    layer_quadras,
                    nova_geom_quadra,
                    id_ignorar=q_id
                )

                if id_quadras_colidida is not None:
                    print(
                        f'Edificação ID {feat_edif.id()} ignorada: '
                        f'invasão detectada com a quadra vizinha ID {id_quadras_colidida}.'
                    )
                    continue

                index_quadras.deleteFeature(quadra_valida)
                layer_quadras.changeGeometry(q_id, nova_geom_quadra)
                quadra_valida.setGeometry(nova_geom_quadra)
                index_quadras.addFeature(quadra_valida)

            if q_id not in edificacoes_por_quadra:
                edificacoes_por_quadra[q_id] = []
            edificacoes_por_quadra[q_id].append(feat_edif)

        # Criação de Lotes para o setor atual
        novas_features_lote = []
        for q_id, lista_edif in edificacoes_por_quadra.items():
            quadra = quadras_in_bbox[q_id]
            str_sq = str(quadra.attribute(idx_q_sq)).strip() if quadra.attribute(idx_q_sq) not in (None, NULL) else ""
            if len(str_sq) != 7:
                print(f'Quadra ID {q_id}: SQ inválido ({str_sq}).')
                continue
                
            cod_lf_atual = max_lf_dict.get(str_sq, 0) + 1
            
            for feat_edif in lista_edif: 
                if cod_lf_atual > 9999:
                    print(f'Quadra {str_sq}: limite de lotes atingido.')
                    break

                str_lf_atual = str(cod_lf_atual).zfill(4)
                str_sql_atual = f"{str_sq}{str_lf_atual}" 

                if len(str_sql_atual) != 11:
                    print(f'SQL inválido: {str_sql_atual}')
                    cod_lf_atual += 1
                    continue
                
                # O script atribui apenas SQL. SQ do lote e SQLE da
                # edificação são preenchidos automaticamente pelo banco.
                atributos_novos = {idx_e_sql: str_sql_atual}
                mapa_edificacoes_para_atualizar[feat_edif.id()] = atributos_novos

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

                nova_feat_lote = QgsFeature(layer_lotes.fields())
                nova_feat_lote.setGeometry(geom_lote)
                
                if idx_l_sql != -1: nova_feat_lote[idx_l_sql] = str_sql_atual
                if idx_l_lf != -1: nova_feat_lote[idx_l_lf] = cod_lf_atual
                
                novas_features_lote.append(nova_feat_lote)
                cod_lf_atual += 1

        # Acumula as inserções/atualizações na sessão de edição aberta
        if novas_features_lote:
            layer_lotes.addFeatures(novas_features_lote)
            total_lotes_criados += len(novas_features_lote)

        if mapa_edificacoes_para_atualizar:
            for fid, atributos in mapa_edificacoes_para_atualizar.items():
                feat_update = layer_edif.getFeature(fid)
                if feat_update.isValid():
                    for idx_campo, novo_valor in atributos.items():
                        feat_update.setAttribute(idx_campo, novo_valor)
                    
                    sucesso = layer_edif.updateFeature(feat_update)
                    if not sucesso:
                        valores_debug = [f"Valor: '{v}' (Tamanho: {len(str(v))})" for v in atributos.values()]
                        print(f"ERRO: O QGIS bloqueou a atualização da Edificação {fid}. {valores_debug}")
                        
            total_edif_atualizadas += len(mapa_edificacoes_para_atualizar)

    # Atualiza a tela ao finalizar todos os setores
    layer_lotes.triggerRepaint()
    layer_edif.triggerRepaint()
    layer_quadras.triggerRepaint()

    if total_lotes_criados > 0 or total_edif_atualizadas > 0 or total_quadras_criadas > 0:
        iface.messageBar().pushMessage(
            "Sucesso Geral",
            f"Processo concluído para todos os setores! {total_quadras_criadas} Quadra(s) e {total_lotes_criados} Lote(s) criado(s), além de {total_edif_atualizadas} Edificação(ões) atualizada(s). Revise e salve manualmente.",
            level=Qgis.Success,
            duration=10
        )
    else:
        iface.messageBar().pushMessage("Concluído", "Nenhuma edificação atendeu aos critérios nos setores analisados.", level=Qgis.Info, duration=5)

extrair_lotes_todos_os_setores()
