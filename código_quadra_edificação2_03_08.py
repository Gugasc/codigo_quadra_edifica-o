from qgis.core import (
    QgsProject, 
    QgsFeature, 
    QgsFeatureRequest,
    QgsSpatialIndex,
    QgsGeometry,
    Qgis
)
from qgis.utils import iface
from qgis.PyQt.QtWidgets import QInputDialog

# ==========================================
# 1. NOMES DAS CAMADAS NO PROJETO DO QGIS
# ==========================================
NOME_CAMADA_EDIF = 'edificacao_fiscal — ct_edificacao_fiscal'
NOME_CAMADA_LOTES = 'lote_fiscal — ct_lote_fiscal'
NOME_CAMADA_QUADRAS = 'quadra_fiscal — ct_quadra_fiscal'
NOME_CAMADA_SETORES = 'ct_setor_fiscal'

NOME_CAMPO_SETOR = 'cod_sf' 

def obter_camada_do_projeto(nome_camada):
    camadas = QgsProject.instance().mapLayersByName(nome_camada)
    if not camadas:
        return None
    return camadas[0]

def pegar_maior_qf_do_setor(layer, valor_sf_sat):
    """ Busca o maior cod_qf na camada apenas para o setor satélite informado """
    maior_qf = 0
    if layer.fields().indexOf('cod_sf_sat') == -1 or layer.fields().indexOf('cod_qf') == -1:
        return maior_qf
        
    expressao = f'"cod_sf_sat" = \'{valor_sf_sat}\' OR "cod_sf_sat" = {valor_sf_sat}'
    request = QgsFeatureRequest().setFilterExpression(expressao)
    
    for feat in layer.getFeatures(request):
        valor = feat['cod_qf']
        if valor and str(valor).isdigit():
            maior_qf = max(maior_qf, int(valor))
            
    return maior_qf

def pegar_maior_valor_geral(layer, nome_campo):
    """ Busca o maior valor absoluto de um campo (como id ou fid) na camada inteira """
    maior_valor = 0
    if layer.fields().indexOf(nome_campo) == -1:
        return maior_valor
        
    for feat in layer.getFeatures():
        valor = feat[nome_campo]
        if valor is not None and str(valor).isdigit():
            maior_valor = max(maior_valor, int(valor))
            
    return maior_valor

def extrair_lotes_e_quadras_por_setor():
    project = QgsProject.instance()
    
    # 2. Carrega as camadas do projeto
    iface.messageBar().pushMessage("Aguarde", "Verificando camadas no projeto...", level=Qgis.Info, duration=2)
    
    layer_edif = obter_camada_do_projeto(NOME_CAMADA_EDIF)
    layer_lotes = obter_camada_do_projeto(NOME_CAMADA_LOTES)
    layer_quadras = obter_camada_do_projeto(NOME_CAMADA_QUADRAS)
    layer_setores = obter_camada_do_projeto(NOME_CAMADA_SETORES)
    
    if not layer_edif or not layer_lotes or not layer_quadras or not layer_setores:
        iface.messageBar().pushMessage("Erro de Camada", "Não foi possível encontrar todas as camadas. Verifique os nomes.", level=Qgis.Critical, duration=7)
        return

    # 3. Pede o código do setor fiscal ao usuário
    numero_digitado, ok = QInputDialog.getText(None, "Filtrar Setor", "Digite o código do setor (cod_sf):")
    if not ok or not numero_digitado.strip():
        iface.messageBar().pushMessage("Cancelado", "Nenhum setor foi digitado.", level=Qgis.Warning, duration=3)
        return
    
    setores_digitados = [numero_digitado]
    
    # 4. Busca o setor fiscal correspondente
    expressao = f'"{NOME_CAMPO_SETOR}" = {numero_digitado}'
    request_setor = QgsFeatureRequest().setFilterExpression(expressao)
    setores = list(layer_setores.getFeatures(request_setor))
    
    if not setores:
        iface.messageBar().pushMessage("Erro", f"Nenhum setor encontrado com o código {numero_digitado}.", level=Qgis.Critical, duration=5)
        return

    setor_selecionado = setores[0]
    valor_sf_sat = setor_selecionado['cod_sf_sat']

    # 5. Descobre os maiores valores existentes para continuar as sequências
    iface.messageBar().pushMessage("Aguarde", f"Calculando contadores e identificadores...", level=Qgis.Info, duration=3)
    
    maior_qf = max(
        pegar_maior_qf_do_setor(layer_quadras, valor_sf_sat),
        pegar_maior_qf_do_setor(layer_lotes, valor_sf_sat)
    )
    proximo_qf = maior_qf + 1

    proximo_id_lote = pegar_maior_valor_geral(layer_lotes, 'id')
    proximo_fid_lote = pegar_maior_valor_geral(layer_lotes, 'fid')
    
    proximo_id_quadra = pegar_maior_valor_geral(layer_quadras, 'id')
    proximo_fid_quadra = pegar_maior_valor_geral(layer_quadras, 'fid')

    # 6. Criação dos Índices Espaciais
    index_edif = QgsSpatialIndex(layer_edif.getFeatures()) 
    index_lotes = QgsSpatialIndex(layer_lotes.getFeatures())
    index_quadras = QgsSpatialIndex(layer_quadras.getFeatures())

    novas_features_lote = []
    novas_features_quadra = []
    edificacoes_para_atualizar = [] 
    edificacoes_processadas = set() 

    # 7. Varredura principal
    for setor in setores:
        geom_setor = setor.geometry()
        bbox_setor = geom_setor.boundingBox()
        
        ids_edificacoes_no_setor = index_edif.intersects(bbox_setor)
        edificacoes_isoladas = []
        
        # --- Coleta apenas edificações que não tocam lotes/quadras ---
        for id_edif in ids_edificacoes_no_setor:
            if id_edif in edificacoes_processadas:
                continue 
                
            feat_edif = layer_edif.getFeature(id_edif)
            geom_edif = feat_edif.geometry()
            
            if not geom_edif.within(geom_setor):
                continue
                
            bbox_edif = geom_edif.boundingBox()

            # Descarta se já tocar em Lote ou Quadra existente
            toca_lote = any(geom_edif.intersects(layer_lotes.getFeature(id_l).geometry()) for id_l in index_lotes.intersects(bbox_edif))
            if toca_lote: continue
            
            toca_quadra = any(geom_edif.intersects(layer_quadras.getFeature(id_q).geometry()) for id_q in index_quadras.intersects(bbox_edif))
            if toca_quadra: continue

            edificacoes_isoladas.append(feat_edif)
            edificacoes_processadas.add(id_edif)

        # --- Agrupa as edificações que se tocam ---
        grupos = []
        visitados = set()
        
        for feat in edificacoes_isoladas:
            if feat.id() in visitados: continue
            
            grupo_atual = [feat]
            visitados.add(feat.id())
            
            # Fila para checar interseções em cadeia
            fila = [feat]
            while fila:
                atual = fila.pop(0)
                geom_atual = atual.geometry()
                
                for outra_feat in edificacoes_isoladas:
                    if outra_feat.id() in visitados: continue
                    
                    # Se as edificações se tocam, pertencem ao mesmo grupo
                    if geom_atual.intersects(outra_feat.geometry()):
                        visitados.add(outra_feat.id())
                        grupo_atual.append(outra_feat)
                        fila.append(outra_feat)
            
            grupos.append(grupo_atual)

        # --- Cria uma Quadra e Múltiplos Lotes ---
        for grupo in grupos:
            # Cria a geometria combinada APENAS para a quadra
            geom_combinada = QgsGeometry(grupo[0].geometry())
            for feat in grupo[1:]:
                geom_combinada = geom_combinada.combine(feat.geometry())

            proximo_id_quadra += 1
            proximo_fid_quadra += 1

            # Códigos base da Quadra
            str_sf_sat = str(valor_sf_sat).zfill(3)
            str_qf_atual = str(proximo_qf).zfill(4)
            str_sq_atual = f"{str_sf_sat}{str_qf_atual}"

            # Prepara a nova feição para Quadra
            nova_feat_quadra = QgsFeature(layer_quadras.fields())
            nova_feat_quadra.setGeometry(geom_combinada)
            if nova_feat_quadra.fields().indexOf('sq') != -1: nova_feat_quadra['sq'] = str_sq_atual
            if nova_feat_quadra.fields().indexOf('cod_sf_sat') != -1: nova_feat_quadra['cod_sf_sat'] = valor_sf_sat
            if nova_feat_quadra.fields().indexOf('cod_qf') != -1: nova_feat_quadra['cod_qf'] = proximo_qf
            if nova_feat_quadra.fields().indexOf('cod_sf') != -1: nova_feat_quadra['cod_sf'] = int(numero_digitado) 
            if nova_feat_quadra.fields().indexOf('id') != -1: nova_feat_quadra['id'] = proximo_id_quadra
            if nova_feat_quadra.fields().indexOf('fid') != -1: nova_feat_quadra['fid'] = proximo_fid_quadra
            novas_features_quadra.append(nova_feat_quadra)

            # Processa cada edificação individualmente para criar seu Lote
            cod_lf_atual = 1 # O contador de lotes reinicia para cada nova quadra
            
            for feat_edif in grupo:
                proximo_id_lote += 1
                proximo_fid_lote += 1

                # Gera o SQL e SQLE específico deste lote
                str_lf_atual = str(cod_lf_atual).zfill(3)
                str_sql_atual = f"{str_sq_atual}{str_lf_atual}"
                str_sqle_atual = f"{str_sql_atual}01"

                # Atualiza a Edificação
                if feat_edif.fields().indexOf('sql') != -1: feat_edif['sql'] = str_sql_atual
                if feat_edif.fields().indexOf('sqle') != -1: feat_edif['sqle'] = str_sqle_atual
                if feat_edif.fields().indexOf('cod_ef') != -1: feat_edif['cod_ef'] = 1
                
                edificacoes_para_atualizar.append(feat_edif)

                # Cria o Lote usando a geometria individual da edificação
                nova_feat_lote = QgsFeature(layer_lotes.fields())
                nova_feat_lote.setGeometry(feat_edif.geometry()) 
                if nova_feat_lote.fields().indexOf('sq') != -1: nova_feat_lote['sq'] = str_sq_atual
                if nova_feat_lote.fields().indexOf('sql') != -1: nova_feat_lote['sql'] = str_sql_atual
                if nova_feat_lote.fields().indexOf('cod_sf_sat') != -1: nova_feat_lote['cod_sf_sat'] = valor_sf_sat
                if nova_feat_lote.fields().indexOf('cod_qf') != -1: nova_feat_lote['cod_qf'] = proximo_qf
                if nova_feat_lote.fields().indexOf('cod_lf') != -1: nova_feat_lote['cod_lf'] = cod_lf_atual
                if nova_feat_lote.fields().indexOf('id') != -1: nova_feat_lote['id'] = proximo_id_lote
                if nova_feat_lote.fields().indexOf('fid') != -1: nova_feat_lote['fid'] = proximo_fid_lote
                novas_features_lote.append(nova_feat_lote)

                cod_lf_atual += 1 # Vai para o próximo lote na mesma quadra

            # Incrementa o sequencial da quadra para o próximo grupo
            proximo_qf += 1

    # 8. Adiciona os novos polígonos e atualiza as edificações existentes
    if novas_features_lote or novas_features_quadra or edificacoes_para_atualizar:
        
        if novas_features_lote:
            layer_lotes.startEditing()
            layer_lotes.addFeatures(novas_features_lote)
            layer_lotes.triggerRepaint()

        if novas_features_quadra:
            layer_quadras.startEditing()
            layer_quadras.addFeatures(novas_features_quadra)
            layer_quadras.triggerRepaint()

        if edificacoes_para_atualizar:
            layer_edif.startEditing()
            for feat in edificacoes_para_atualizar:
                layer_edif.updateFeature(feat) 
            layer_edif.triggerRepaint()

        iface.messageBar().pushMessage(
            "Sucesso", 
            f"Processado: {len(novas_features_quadra)} Quadras, {len(novas_features_lote)} Lotes e {len(edificacoes_para_atualizar)} Edificações atualizadas!", 
            level=Qgis.Success, 
            duration=7
        )
    else:
        iface.messageBar().pushMessage("Concluído", "Nenhuma edificação isolada precisou ser alterada.", level=Qgis.Info, duration=5)

extrair_lotes_e_quadras_por_setor()